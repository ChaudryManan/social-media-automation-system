"""Web search provider interface and implementations (``ARCHITECTURE.md`` 7).

``SearchProvider.search(query)`` returns a list of :class:`SearchResult`. The
provider is chosen by the ``SEARCH_PROVIDER`` setting through
:func:`get_search_provider`:

* ``fake`` (default): offline, deterministic, scriptable (:class:`FakeSearch`).
* ``tavily``: the Tavily search API over ``httpx`` (needs ``TAVILY_API_KEY``).

Agents never import an HTTP client or a search SDK; they call this module only.
Queries and result text are never logged (the log line carries provider, result
count and latency only).
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 1000
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SECONDS = 20.0


class SearchError(AppError):
    """Base class for search layer failures."""

    def __init__(
        self,
        message: str = "Search failed",
        code: str = "search_error",
        http_status: int = 502,
        retriable: bool = True,
    ) -> None:
        super().__init__(code=code, message=message, http_status=http_status)
        self.retriable = retriable


class SearchConfigError(SearchError):
    """The search provider is unknown or not configured (never retriable)."""

    def __init__(self, message: str = "Search is not configured") -> None:
        super().__init__(
            message=message,
            code="search_not_configured",
            http_status=500,
            retriable=False,
        )


class SearchProviderError(SearchError):
    """The provider call failed (network, quota, server error...)."""

    def __init__(
        self, message: str = "Search provider call failed", retriable: bool = True
    ) -> None:
        super().__init__(
            message=message,
            code="search_provider_error",
            http_status=502,
            retriable=retriable,
        )


@dataclass(frozen=True)
class SearchResult:
    """One search hit. ``snippet`` is trimmed to ``MAX_SNIPPET_CHARS``."""

    title: str
    url: str
    snippet: str


class SearchProvider(ABC):
    """Interface every search provider implements."""

    name: str

    @abstractmethod
    def search(self, query: str) -> list[SearchResult]:
        """Return up to the provider's result limit for ``query`` (may be empty)."""


def _clip(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


# --------------------------------------------------------------------------
# Fake provider
# --------------------------------------------------------------------------


def _slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.casefold()).strip("-")[:60] or "query"


class FakeSearch(SearchProvider):
    """Offline provider for tests and development.

    Without a script it answers every query with two deterministic results
    derived from the query. ``set_results`` replaces that default, ``queue``
    gives per-call answers (consumed in order), ``fail_next(n)`` makes the next
    ``n`` calls raise :class:`SearchProviderError`. Every query is recorded in
    ``calls``.
    """

    name = "fake"

    def __init__(self, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self.max_results = max_results
        self.calls: list[str] = []
        self._default: list[SearchResult] | None = None
        self._queue: list[list[SearchResult]] = []
        self._failures = 0
        self._failure_retriable = True

    def set_results(self, results: list[SearchResult] | None) -> None:
        self._default = None if results is None else list(results)

    def queue(self, *answers: list[SearchResult]) -> None:
        self._queue.extend(list(a) for a in answers)

    def fail_next(self, count: int = 1, *, retriable: bool = True) -> None:
        self._failures = count
        self._failure_retriable = retriable

    def reset(self) -> None:
        self.calls.clear()
        self._default = None
        self._queue.clear()
        self._failures = 0
        self._failure_retriable = True

    def search(self, query: str) -> list[SearchResult]:
        self.calls.append(query)
        if self._failures > 0:
            self._failures -= 1
            raise SearchProviderError(
                "Fake search failure", retriable=self._failure_retriable
            )
        if self._queue:
            return self._queue.pop(0)[: self.max_results]
        if self._default is not None:
            return self._default[: self.max_results]
        slug = _slug(query)
        return [
            SearchResult(
                title=f"Fake result {n} for {query}",
                url=f"https://example.com/fake/{slug}/{n}",
                snippet=f"Fake snippet {n} about {query}.",
            )
            for n in (1, 2)
        ][: self.max_results]


_fake_search = FakeSearch()


def get_fake_search() -> FakeSearch:
    """The shared fake provider returned by ``get_search_provider()`` in fake mode."""
    return _fake_search


# --------------------------------------------------------------------------
# Tavily provider
# --------------------------------------------------------------------------


class TavilySearch(SearchProvider):
    """Tavily search API (``POST https://api.tavily.com/search``, Bearer key)."""

    name = "tavily"

    def __init__(
        self,
        api_key: str | None,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        timeout: float = TAVILY_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise SearchConfigError("TAVILY_API_KEY is not set")
        self._api_key = api_key
        self.max_results = max_results
        self._timeout = timeout
        self._client = client

    def search(self, query: str) -> list[SearchResult]:
        started = time.monotonic()
        body = {"query": query, "max_results": self.max_results}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(TAVILY_URL, json=body, headers=headers)
            else:
                response = httpx.post(
                    TAVILY_URL, json=body, headers=headers, timeout=self._timeout
                )
        except httpx.HTTPError as exc:
            # The message never carries the request (it holds the key and query).
            raise SearchProviderError(
                f"Tavily request failed ({type(exc).__name__})"
            ) from None

        if response.status_code in (401, 403):
            raise SearchConfigError("Tavily rejected the API key")
        if response.status_code >= 400:
            retriable = response.status_code == 429 or response.status_code >= 500
            raise SearchProviderError(
                f"Tavily returned HTTP {response.status_code}", retriable=retriable
            )
        results = self._parse(response)
        logger.info(
            "Search finished",
            extra={
                "provider": self.name,
                "results": len(results),
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return results

    def _parse(self, response: httpx.Response) -> list[SearchResult]:
        try:
            payload: Any = response.json()
            items = payload["results"]
        except (ValueError, KeyError, TypeError):
            raise SearchProviderError(
                "Tavily returned an unreadable response", retriable=False
            ) from None
        results: list[SearchResult] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            results.append(
                SearchResult(
                    title=_clip(item.get("title") or url, 200),
                    url=url,
                    snippet=_clip(item.get("content") or ""),
                )
            )
        return results[: self.max_results]


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def get_search_provider(max_results: int = DEFAULT_MAX_RESULTS) -> SearchProvider:
    """The provider selected by ``SEARCH_PROVIDER`` (settings are read per call)."""
    settings = get_settings()
    name = (settings.SEARCH_PROVIDER or "").strip().casefold()
    if name == "fake":
        provider = get_fake_search()
        provider.max_results = max_results
        return provider
    if name == "tavily":
        return TavilySearch(settings.TAVILY_API_KEY, max_results=max_results)
    raise SearchConfigError(f"Unknown SEARCH_PROVIDER '{settings.SEARCH_PROVIDER}'")
