"""Image provider interface and implementations (``ARCHITECTURE.md`` 6.13).

``ImageProvider.generate(prompt, size)`` returns the encoded image bytes. The
provider is chosen by the ``IMAGE_PROVIDER`` setting through
:func:`get_image_provider`:

* ``fake`` (default): offline, deterministic, scriptable (:class:`FakeImage`).
* ``gemini``: Gemini native image generation over ``httpx`` (needs
  ``GEMINI_API_KEY`` and ``IMAGE_MODEL``).

``size`` is a *request* ``(width, height)``. The fake honours it exactly; a real
model picks the closest aspect ratio it supports and decides the resolution, so
callers must read the real dimensions from the bytes with
:func:`read_image_info` (the same helper gives the mime type for storage).

Agents never import an HTTP client or an image SDK; they call this module only.
Prompts are never logged or put in error messages (the log line carries
provider, size in bytes and latency only).
"""

import base64
import binascii
import hashlib
import logging
import math
import re
import struct
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

ImageSize = tuple[int, int]

DEFAULT_IMAGE_SIZE: ImageSize = (1024, 1024)
MIN_DIMENSION = 1
MAX_DIMENSION = 4096

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1"
GEMINI_TIMEOUT_SECONDS = 120.0
# Aspect ratios accepted by every Gemini image model in the public docs
# (some models also take 1:4, 1:8, 4:1 and 8:1; these ten work everywhere).
GEMINI_ASPECT_RATIOS = (
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ImageError(AppError):
    """Base class for image layer failures."""

    def __init__(
        self,
        message: str = "Image generation failed",
        code: str = "image_error",
        http_status: int = 502,
        retriable: bool = True,
    ) -> None:
        super().__init__(code=code, message=message, http_status=http_status)
        self.retriable = retriable


class ImageConfigError(ImageError):
    """The image provider is unknown or not configured (never retriable)."""

    def __init__(self, message: str = "Image generation is not configured") -> None:
        super().__init__(
            message=message,
            code="image_not_configured",
            http_status=500,
            retriable=False,
        )


class InvalidImageRequestError(ImageError):
    """The prompt or size was rejected before any provider call (a caller bug)."""

    def __init__(self, message: str = "Invalid image request") -> None:
        super().__init__(
            message=message,
            code="invalid_image_request",
            http_status=400,
            retriable=False,
        )


class ImageProviderError(ImageError):
    """The provider call failed (network, quota, refusal, unusable reply...)."""

    def __init__(
        self,
        message: str = "Image provider call failed",
        retriable: bool = True,
        code: str = "image_provider_error",
    ) -> None:
        super().__init__(
            message=message, code=code, http_status=502, retriable=retriable
        )


# --------------------------------------------------------------------------
# Image header reading (mime type and dimensions without an imaging library)
# --------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD9)})


@dataclass(frozen=True)
class ImageInfo:
    """What the header of an image says: ``mime`` and pixel size."""

    mime: str
    width: int
    height: int


def detect_image_mime(data: bytes) -> str | None:
    """``image/png``, ``image/jpeg`` or ``image/webp`` by signature, else ``None``."""
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_image_info(data: bytes) -> ImageInfo | None:
    """Mime type and size from the header of a PNG, JPEG or WebP.

    Returns ``None`` when the bytes are not one of those formats or the header
    is truncated or inconsistent. Only the header is read: this says the file
    *claims* to be a valid image of that size, it does not decode the pixels.
    """
    readers = {
        "image/png": _png_size,
        "image/jpeg": _jpeg_size,
        "image/webp": _webp_size,
    }
    mime = detect_image_mime(data)
    if mime is None:
        return None
    size = readers[mime](data)
    if size is None or min(size) < 1:
        return None
    return ImageInfo(mime=mime, width=size[0], height=size[1])


def _png_size(data: bytes) -> ImageSize | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_size(data: bytes) -> ImageSize | None:
    pos, end = 2, len(data)
    while pos + 4 <= end:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in _JPEG_STANDALONE:
            pos += 2
            continue
        if marker in (0x00, 0xD9, 0xDA):  # invalid, end of image, start of scan
            return None
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if marker in _JPEG_SOF:
            if pos + 9 > end:
                return None
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")
            return width, height
        pos += 2 + length
    return None


def _webp_size(data: bytes) -> ImageSize | None:
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------


class ImageProvider(ABC):
    """Interface every image provider implements.

    ``generate`` validates the request once, then calls the provider's
    ``_generate``; every implementation therefore rejects the same bad input.
    """

    name: str

    def generate(self, prompt: str, size: ImageSize = DEFAULT_IMAGE_SIZE) -> bytes:
        """Return encoded image bytes (PNG, JPEG or WebP) for ``prompt``."""
        return self._generate(_clean_prompt(prompt), _validate_size(size))

    @abstractmethod
    def _generate(self, prompt: str, size: ImageSize) -> bytes: ...


def _clean_prompt(prompt: object) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidImageRequestError("The image prompt is empty")
    return prompt.strip()


def _validate_size(size: object) -> ImageSize:
    ok = (
        isinstance(size, tuple)
        and len(size) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in size)
        and all(MIN_DIMENSION <= v <= MAX_DIMENSION for v in size)
    )
    if not ok:
        raise InvalidImageRequestError(
            f"Image size must be (width, height) in {MIN_DIMENSION}..{MAX_DIMENSION}"
        )
    return size  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Fake provider
# --------------------------------------------------------------------------


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def build_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """A valid single-colour PNG built with the standard library only.

    It is a 1-bit palette image (one bit per pixel, all pixels use the first
    palette colour), so even 4096x4096 is a few kilobytes.
    """
    header = struct.pack(">IIBBBBB", width, height, 1, 3, 0, 0, 0)
    palette = bytes(color) + b"\xff\xff\xff"
    row = b"\x00" * (1 + (width + 7) // 8)  # filter byte 0 + all-zero pixel bits
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"PLTE", palette)
        + _png_chunk(b"IDAT", zlib.compress(row * height, 9))
        + _png_chunk(b"IEND", b"")
    )


class FakeImage(ImageProvider):
    """Offline provider for tests and development.

    Without a script it returns a PNG of exactly the requested size whose
    colour is derived from the prompt (same prompt, same bytes). ``queue``
    gives per-call answers (consumed in order, e.g. broken bytes to exercise an
    image check), ``fail_next(n)`` makes the next ``n`` calls raise
    :class:`ImageProviderError`. Every call is recorded in ``calls`` as
    ``(prompt, size)``.
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ImageSize]] = []
        self._queue: list[bytes] = []
        self._failures = 0
        self._failure_retriable = True

    def queue(self, *images: bytes) -> None:
        self._queue.extend(images)

    def fail_next(self, count: int = 1, *, retriable: bool = True) -> None:
        self._failures = count
        self._failure_retriable = retriable

    def reset(self) -> None:
        self.calls.clear()
        self._queue.clear()
        self._failures = 0
        self._failure_retriable = True

    def _generate(self, prompt: str, size: ImageSize) -> bytes:
        self.calls.append((prompt, size))
        if self._failures > 0:
            self._failures -= 1
            raise ImageProviderError(
                "Fake image failure", retriable=self._failure_retriable
            )
        if self._queue:
            return self._queue.pop(0)
        digest = hashlib.sha256(prompt.encode("utf-8")).digest()
        return build_png(size[0], size[1], (digest[0], digest[1], digest[2]))


_fake_image = FakeImage()


def get_fake_image() -> FakeImage:
    """The shared fake provider returned by ``get_image_provider()`` in fake mode."""
    return _fake_image


# --------------------------------------------------------------------------
# Gemini provider
# --------------------------------------------------------------------------


def nearest_aspect_ratio(size: ImageSize) -> str:
    """The Gemini aspect ratio closest to ``size`` (compared on a log scale)."""
    target = math.log(size[0] / size[1])

    def distance(ratio: str) -> float:
        w, h = (int(part) for part in ratio.split(":"))
        return abs(math.log(w / h) - target)

    return min(GEMINI_ASPECT_RATIOS, key=distance)


class GeminiImage(ImageProvider):
    """Gemini native image generation (``models/{model}:generateContent``).

    The request is ``{"contents": [...], "generationConfig": {"responseModalities":
    ["IMAGE"], "responseFormat": {"image": {"aspectRatio": ...}}}}`` with the key
    in the ``x-goog-api-key`` header; the image is the first non-thought part
    with ``inlineData`` (base64). Contract taken from the public Gemini API
    docs (checked 2026-11-21); no live call has been made yet.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str | None,
        model: str | None,
        *,
        base_url: str = GEMINI_BASE_URL,
        timeout: float = GEMINI_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ImageConfigError("GEMINI_API_KEY is not set")
        model = (model or "").strip()
        if not model:
            raise ImageConfigError("IMAGE_MODEL is not set")
        if not _MODEL_NAME.match(model):
            raise ImageConfigError("IMAGE_MODEL contains invalid characters")
        self._api_key = api_key
        self.model = model
        self._url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
        self._timeout = timeout
        self._client = client

    def _generate(self, prompt: str, size: ImageSize) -> bytes:
        started = time.monotonic()
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "responseFormat": {
                    "image": {"aspectRatio": nearest_aspect_ratio(size)}
                },
            },
        }
        headers = {"x-goog-api-key": self._api_key}
        try:
            if self._client is not None:
                response = self._client.post(self._url, json=body, headers=headers)
            else:
                response = httpx.post(
                    self._url, json=body, headers=headers, timeout=self._timeout
                )
        except httpx.HTTPError as exc:
            # The message never carries the request (it holds the key and prompt).
            raise ImageProviderError(
                f"Gemini request failed ({type(exc).__name__})"
            ) from None

        if response.status_code in (401, 403):
            raise ImageConfigError("Gemini rejected the API key")
        if response.status_code == 404:
            raise ImageConfigError("Gemini does not know this IMAGE_MODEL")
        if response.status_code >= 400:
            retriable = response.status_code == 429 or response.status_code >= 500
            raise ImageProviderError(
                f"Gemini returned HTTP {response.status_code}", retriable=retriable
            )
        image = self._parse(response)
        logger.info(
            "Image generated",
            extra={
                "provider": self.name,
                "size_bytes": len(image),
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return image

    def _parse(self, response: httpx.Response) -> bytes:
        try:
            payload: Any = response.json()
        except ValueError:
            raise ImageProviderError(
                "Gemini returned an unreadable response", retriable=False
            ) from None
        if not isinstance(payload, dict):
            raise ImageProviderError(
                "Gemini returned an unreadable response", retriable=False
            )

        candidates = payload.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict) or part.get("thought"):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return self._decode(inline["data"])

        feedback = payload.get("promptFeedback")
        blocked = isinstance(feedback, dict) and feedback.get("blockReason")
        finish = candidate.get("finishReason") if isinstance(candidate, dict) else None
        if blocked or finish not in (None, "STOP"):
            raise ImageProviderError(
                "Gemini declined to generate this image",
                retriable=False,
                code="image_refused",
            )
        raise ImageProviderError("Gemini returned no image")

    @staticmethod
    def _decode(data: object) -> bytes:
        try:
            # Line-wrapping whitespace is harmless; any other stray character
            # must fail instead of being dropped (that would corrupt the image).
            image = base64.b64decode("".join(str(data).split()), validate=True)
        except (binascii.Error, ValueError):
            raise ImageProviderError(
                "Gemini returned an unreadable image", retriable=False
            ) from None
        if detect_image_mime(image) is None:
            raise ImageProviderError(
                "Gemini returned data that is not a PNG, JPEG or WebP image",
                retriable=False,
            )
        return image


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def get_image_provider() -> ImageProvider:
    """The provider selected by ``IMAGE_PROVIDER`` (settings are read per call)."""
    settings = get_settings()
    name = (settings.IMAGE_PROVIDER or "").strip().casefold()
    if name == "fake":
        return get_fake_image()
    if name == "gemini":
        return GeminiImage(settings.GEMINI_API_KEY, settings.IMAGE_MODEL)
    raise ImageConfigError(f"Unknown IMAGE_PROVIDER '{settings.IMAGE_PROVIDER}'")
