"""Tests for ``app.providers.search`` (B2-06): fake, Tavily (mocked HTTP), registry."""

import json
from typing import Any

import httpx
import pytest
from app.core.config import get_settings
from app.providers.search import (
    MAX_SNIPPET_CHARS,
    TAVILY_URL,
    FakeSearch,
    SearchConfigError,
    SearchProviderError,
    SearchResult,
    TavilySearch,
    get_fake_search,
    get_search_provider,
)


def result(n: int = 1) -> SearchResult:
    return SearchResult(title=f"T{n}", url=f"https://example.com/{n}", snippet=f"S{n}")


# --------------------------------------------------------------------------
# FakeSearch
# --------------------------------------------------------------------------


def test_the_fake_answers_deterministically_without_a_script() -> None:
    fake = FakeSearch()

    first = fake.search("Green Tea benefits!")

    assert first == fake.search("Green Tea benefits!")
    assert len(first) == 2
    assert first[0].url == "https://example.com/fake/green-tea-benefits/1"
    assert fake.calls == ["Green Tea benefits!", "Green Tea benefits!"]


def test_the_fake_slug_is_never_empty() -> None:
    assert "/fake/query/" in FakeSearch().search("!!!")[0].url


def test_set_results_replaces_the_default_and_respects_max_results() -> None:
    fake = FakeSearch(max_results=2)
    fake.set_results([result(1), result(2), result(3)])

    assert fake.search("q") == [result(1), result(2)]
    fake.set_results(None)
    assert fake.search("q")[0].url.startswith("https://example.com/fake/")


def test_queued_answers_are_used_once_in_order_then_the_default() -> None:
    fake = FakeSearch()
    fake.set_results([result(9)])
    fake.queue([result(1)], [])

    assert fake.search("a") == [result(1)]
    assert fake.search("b") == []
    assert fake.search("c") == [result(9)]


def test_fail_next_raises_that_many_times_then_recovers() -> None:
    fake = FakeSearch()
    fake.fail_next(2, retriable=False)

    for _ in range(2):
        with pytest.raises(SearchProviderError) as caught:
            fake.search("q")
        assert caught.value.retriable is False
    assert fake.search("q")


def test_reset_clears_everything() -> None:
    fake = FakeSearch()
    fake.set_results([result()])
    fake.queue([result(2)])
    fake.fail_next(1)
    fake.reset()

    assert fake.calls == []
    assert fake.search("q")[0].url.startswith("https://example.com/fake/")


# --------------------------------------------------------------------------
# TavilySearch (HTTP mocked with httpx.MockTransport)
# --------------------------------------------------------------------------


def tavily(handler: Any, **kwargs: Any) -> TavilySearch:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TavilySearch("tvly-secret", client=client, **kwargs)


def ok_payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"query": "q", "results": list(items)}


def test_tavily_requires_a_key() -> None:
    with pytest.raises(SearchConfigError) as caught:
        TavilySearch(None)
    assert caught.value.code == "search_not_configured"
    assert caught.value.retriable is False


def test_tavily_sends_the_documented_request_and_parses_results() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=ok_payload(
                {"title": "Tea", "url": "https://a.com/x", "content": "About  tea"},
                {"title": "", "url": "https://b.com/y", "content": "More"},
            ),
        )

    results = tavily(handler, max_results=3).search("green tea")

    assert seen["url"] == TAVILY_URL
    assert seen["auth"] == "Bearer tvly-secret"
    assert seen["body"] == {"query": "green tea", "max_results": 3}
    assert results == [
        SearchResult("Tea", "https://a.com/x", "About tea"),
        SearchResult("https://b.com/y", "https://b.com/y", "More"),  # title fallback
    ]


def test_tavily_skips_bad_items_and_trims_long_snippets() -> None:
    long_text = "word " * 1000
    payload = ok_payload(
        {"title": "ok", "url": "https://a.com", "content": long_text},
        {"title": "no url", "content": "x"},
        {"title": "bad scheme", "url": "javascript:alert(1)", "content": "x"},
        "not a dict",
    )

    (hit,) = tavily(lambda r: httpx.Response(200, json=payload)).search("q")

    assert len(hit.snippet) <= MAX_SNIPPET_CHARS
    assert hit.snippet.endswith("\u2026")


def test_tavily_caps_results_at_max_results() -> None:
    items = [
        {"title": str(n), "url": f"https://a.com/{n}", "content": ""} for n in range(9)
    ]

    hits = tavily(
        lambda r: httpx.Response(200, json=ok_payload(*items)), max_results=4
    ).search("q")

    assert len(hits) == 4


@pytest.mark.parametrize("status", [401, 403])
def test_tavily_key_rejection_is_a_config_error(status: int) -> None:
    with pytest.raises(SearchConfigError):
        tavily(lambda r: httpx.Response(status)).search("q")


@pytest.mark.parametrize(
    ("status", "retriable"),
    [(429, True), (500, True), (503, True), (400, False), (404, False)],
)
def test_tavily_http_errors_set_retriable(status: int, retriable: bool) -> None:
    with pytest.raises(SearchProviderError) as caught:
        tavily(lambda r: httpx.Response(status)).search("q")

    assert caught.value.retriable is retriable
    assert str(status) in caught.value.message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"no": "results"}),
        httpx.Response(200, json=["list"]),
    ],
)
def test_tavily_unreadable_replies_are_non_retriable_errors(
    response: httpx.Response,
) -> None:
    with pytest.raises(SearchProviderError) as caught:
        tavily(lambda r: response).search("q")

    assert caught.value.retriable is False


def test_tavily_non_list_results_mean_no_results() -> None:
    assert (
        tavily(lambda r: httpx.Response(200, json={"results": None})).search("q") == []
    )


def test_tavily_network_errors_are_retriable_and_never_leak_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom tvly-secret", request=request)

    with pytest.raises(SearchProviderError) as caught:
        tavily(handler).search("private query")

    assert caught.value.retriable is True
    assert "tvly-secret" not in caught.value.message
    assert "private query" not in caught.value.message
    assert caught.value.__cause__ is None


def test_tavily_without_an_injected_client_uses_httpx_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        return httpx.Response(200, json=ok_payload())

    monkeypatch.setattr(httpx, "post", fake_post)

    assert TavilySearch("k", timeout=3.0).search("q") == []
    assert calls[0]["url"] == TAVILY_URL
    assert calls[0]["timeout"] == 3.0


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def set_env(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield set_env
    get_settings.cache_clear()


def test_the_registry_returns_the_shared_fake_and_sets_its_limit(env: Any) -> None:
    env(SEARCH_PROVIDER="fake")

    provider = get_search_provider(3)

    assert provider is get_fake_search()
    assert provider.max_results == 3  # type: ignore[attr-defined]
    get_fake_search().max_results = 5


def test_the_registry_is_case_insensitive(env: Any) -> None:
    env(SEARCH_PROVIDER=" FAKE ")

    assert get_search_provider() is get_fake_search()


def test_the_registry_builds_tavily_with_the_key_from_settings(env: Any) -> None:
    env(SEARCH_PROVIDER="tavily", TAVILY_API_KEY="tvly-1")

    provider = get_search_provider(2)

    assert isinstance(provider, TavilySearch)
    assert provider.max_results == 2


def test_tavily_without_a_key_in_settings_is_a_config_error(env: Any) -> None:
    env(SEARCH_PROVIDER="tavily", TAVILY_API_KEY="")

    with pytest.raises(SearchConfigError):
        get_search_provider()


def test_an_unknown_provider_is_a_config_error(env: Any) -> None:
    env(SEARCH_PROVIDER="bing")

    with pytest.raises(SearchConfigError) as caught:
        get_search_provider()

    assert "bing" in caught.value.message
