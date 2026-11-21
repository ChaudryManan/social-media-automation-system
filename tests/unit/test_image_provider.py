"""Tests for ``app.providers.image`` (B2-09): fake, header readers, Gemini, registry."""

import base64
import json
import logging
import struct
import zlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from app.core.config import get_settings
from app.providers.image import (
    DEFAULT_IMAGE_SIZE,
    GEMINI_ASPECT_RATIOS,
    MAX_DIMENSION,
    PNG_SIGNATURE,
    FakeImage,
    GeminiImage,
    ImageConfigError,
    ImageInfo,
    ImageProvider,
    ImageProviderError,
    InvalidImageRequestError,
    build_png,
    detect_image_mime,
    get_fake_image,
    get_image_provider,
    nearest_aspect_ratio,
    read_image_info,
)

# --------------------------------------------------------------------------
# Helpers: independent PNG parsing and hand-made JPEG / WebP headers
# --------------------------------------------------------------------------


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Split a PNG into (type, payload), verifying every CRC."""
    assert data.startswith(PNG_SIGNATURE)
    pos, chunks = len(PNG_SIGNATURE), []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"bad CRC in {kind!r}"
        chunks.append((kind, payload))
        pos += 12 + length
    assert pos == len(data)
    return chunks


def jpeg(width: int, height: int, sof: int = 0xC0, before: bytes = b"") -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = (
        bytes([0xFF, sof])
        + b"\x00\x11\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + before + app0 + frame + b"\xff\xd9"


def riff(kind: bytes, body: bytes) -> bytes:
    chunk = kind + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_vp8x(width: int, height: int) -> bytes:
    body = b"\x00" * 4 + (width - 1).to_bytes(3, "little")
    return riff(b"VP8X", body + (height - 1).to_bytes(3, "little"))


def webp_vp8(width: int, height: int) -> bytes:
    body = b"\x30\x01\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return riff(b"VP8 ", body)


def webp_vp8l(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return riff(b"VP8L", b"\x2f" + struct.pack("<I", bits))


# --------------------------------------------------------------------------
# build_png and FakeImage
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size", [(1, 1), (7, 13), (64, 64), (1080, 1350), (4096, 4096)]
)
def test_build_png_is_a_structurally_valid_png_of_the_requested_size(
    size: tuple[int, int],
) -> None:
    width, height = size
    data = build_png(width, height, (10, 20, 30))

    chunks = png_chunks(data)  # signature, framing and every CRC

    assert [kind for kind, _ in chunks] == [b"IHDR", b"PLTE", b"IDAT", b"IEND"]
    ihdr = dict(chunks)[b"IHDR"]
    assert struct.unpack(">IIBBBBB", ihdr) == (width, height, 1, 3, 0, 0, 0)
    assert dict(chunks)[b"PLTE"][:3] == bytes([10, 20, 30])
    pixels = zlib.decompress(dict(chunks)[b"IDAT"])
    row = 1 + (width + 7) // 8
    assert len(pixels) == row * height
    assert all(pixels[r * row] == 0 for r in range(height))  # filter type "none"
    assert not any(
        pixels[r * row + 1 : (r + 1) * row] != b"\x00" * (row - 1)
        for r in range(height)
    )  # every pixel uses palette entry 0


def test_the_fake_returns_a_png_with_the_correct_mime_and_size() -> None:
    data = FakeImage().generate("a red apple", (1080, 1350))

    assert data.startswith(PNG_SIGNATURE)
    assert detect_image_mime(data) == "image/png"
    assert read_image_info(data) == ImageInfo("image/png", 1080, 1350)


def test_the_fake_image_is_small_even_for_a_large_request() -> None:
    assert len(FakeImage().generate("x", (MAX_DIMENSION, MAX_DIMENSION))) < 10_000


def test_the_default_size_is_used_when_none_is_given() -> None:
    fake = FakeImage()

    data = fake.generate("x")

    assert DEFAULT_IMAGE_SIZE == (1024, 1024)
    assert read_image_info(data) == ImageInfo("image/png", 1024, 1024)
    assert fake.calls == [("x", (1024, 1024))]


def test_the_fake_is_deterministic_per_prompt() -> None:
    fake = FakeImage()

    assert fake.generate("same", (8, 8)) == fake.generate("same", (8, 8))
    assert fake.generate("one", (8, 8)) != fake.generate("two", (8, 8))


def test_the_fake_records_the_cleaned_prompt_and_size() -> None:
    fake = FakeImage()

    fake.generate("  a cat \n", (10, 20))

    assert fake.calls == [("a cat", (10, 20))]


def test_queued_images_are_returned_once_in_order_then_the_default() -> None:
    fake = FakeImage()
    fake.queue(b"broken", b"also broken")

    assert fake.generate("x", (4, 4)) == b"broken"
    assert fake.generate("x", (4, 4)) == b"also broken"
    assert fake.generate("x", (4, 4)).startswith(PNG_SIGNATURE)


def test_fail_next_raises_that_many_times_then_recovers() -> None:
    fake = FakeImage()
    fake.fail_next(2, retriable=False)

    for _ in range(2):
        with pytest.raises(ImageProviderError) as caught:
            fake.generate("x")
        assert caught.value.retriable is False
        assert caught.value.code == "image_provider_error"
    assert fake.generate("x").startswith(PNG_SIGNATURE)
    assert len(fake.calls) == 3  # failed calls are recorded too


def test_fail_next_is_retriable_by_default() -> None:
    fake = FakeImage()
    fake.fail_next()

    with pytest.raises(ImageProviderError) as caught:
        fake.generate("x")

    assert caught.value.retriable is True
    assert caught.value.http_status == 502


def test_reset_clears_calls_queue_and_failures() -> None:
    fake = FakeImage()
    fake.queue(b"x")
    fake.fail_next(3, retriable=False)
    fake.calls.append(("p", (1, 1)))

    fake.reset()

    assert fake.calls == []
    assert fake.generate("p", (2, 2)).startswith(PNG_SIGNATURE)
    fake.fail_next()
    fake.reset()
    assert fake.generate("p", (2, 2)).startswith(PNG_SIGNATURE)


# --------------------------------------------------------------------------
# Request validation (shared by every provider)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t", None, 5, b"x", ["x"]])
def test_an_empty_or_non_text_prompt_is_rejected(prompt: Any) -> None:
    fake = FakeImage()

    with pytest.raises(InvalidImageRequestError) as caught:
        fake.generate(prompt)

    assert caught.value.code == "invalid_image_request"
    assert caught.value.http_status == 400
    assert caught.value.retriable is False
    assert fake.calls == []  # the provider never saw it


@pytest.mark.parametrize(
    "size",
    [
        (0, 100),
        (100, 0),
        (-1, 100),
        (MAX_DIMENSION + 1, 100),
        (100, MAX_DIMENSION + 1),
        (100,),
        (100, 100, 3),
        [100, 100],
        (100.0, 100),
        ("100", 100),
        (True, 100),
        None,
        1024,
    ],
)
def test_a_bad_size_is_rejected(size: Any) -> None:
    fake = FakeImage()

    with pytest.raises(InvalidImageRequestError):
        fake.generate("x", size)

    assert fake.calls == []


@pytest.mark.parametrize("size", [(1, 1), (MAX_DIMENSION, MAX_DIMENSION), (4096, 1)])
def test_boundary_sizes_are_accepted(size: tuple[int, int]) -> None:
    assert read_image_info(FakeImage().generate("x", size)) is not None


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ImageProvider()  # type: ignore[abstract]


# --------------------------------------------------------------------------
# detect_image_mime / read_image_info
# --------------------------------------------------------------------------


def test_mime_detection_by_signature() -> None:
    assert detect_image_mime(build_png(1, 1, (0, 0, 0))) == "image/png"
    assert detect_image_mime(jpeg(1, 1)) == "image/jpeg"
    assert detect_image_mime(webp_vp8x(1, 1)) == "image/webp"
    for other in (
        b"",
        b"GIF89a....",
        b"%PDF-1.7",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"\xff\xd8",
    ):
        assert detect_image_mime(other) is None


def test_png_size_is_read_from_ihdr() -> None:
    assert read_image_info(build_png(320, 200, (1, 2, 3))) == ImageInfo(
        "image/png", 320, 200
    )


@pytest.mark.parametrize("sof", [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xCF])
def test_jpeg_size_is_read_from_any_start_of_frame_marker(sof: int) -> None:
    assert read_image_info(jpeg(640, 480, sof=sof)) == ImageInfo("image/jpeg", 640, 480)


def test_jpeg_tables_are_not_mistaken_for_a_frame() -> None:
    dht = b"\xff\xc4\x00\x05\x00\x00\x00"  # define Huffman table: not a SOF
    dac = b"\xff\xcc\x00\x04\x00\x00"  # arithmetic conditioning
    dqt = b"\xff\xdb\x00\x04\x00\x00"

    data = jpeg(800, 600, before=dht + dac + dqt)

    assert read_image_info(data) == ImageInfo("image/jpeg", 800, 600)


def test_jpeg_fill_bytes_and_standalone_markers_are_skipped() -> None:
    data = jpeg(50, 40, before=b"\xff\xff\xff\x01")

    assert read_image_info(data) == ImageInfo("image/jpeg", 50, 40)


def test_a_jpeg_without_a_frame_header_is_not_readable() -> None:
    assert read_image_info(b"\xff\xd8\xff\xda\x00\x02\xff\xd9") is None  # scan first
    assert read_image_info(b"\xff\xd8\xff\xd9") is None
    assert read_image_info(b"\xff\xd8\xff\xe0\x00\x01") is None  # length < 2
    assert read_image_info(b"\xff\xd8\xffnot a marker stream") is None
    assert read_image_info(b"\xff\xd8\xff\x00\x00\x00\x00\x00") is None
    # a valid APP0 segment followed by a byte that is not a marker
    assert read_image_info(b"\xff\xd8\xff\xe0\x00\x04\x00\x00\x12\x34\x56\x78") is None


def test_jpeg_scan_data_is_never_searched_for_a_frame_header() -> None:
    frame = (
        b"\xff\xc0\x00\x11\x08\x00\x0a\x00\x0a\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    scan_first = b"\xff\xd8\xff\xda\x00\x02" + frame  # a "frame" inside entropy data

    assert read_image_info(scan_first) is None
    assert read_image_info(b"\xff\xd8\xff\xd9" + frame) is None  # nothing after EOI


def test_a_truncated_jpeg_frame_header_is_not_readable() -> None:
    assert read_image_info(jpeg(10, 10)[:15]) is None
    assert read_image_info(b"\xff\xd8\xff\xc0\x00\x11\x08\x00") is None


@pytest.mark.parametrize(
    ("builder", "label"),
    [(webp_vp8x, "VP8X"), (webp_vp8, "VP8"), (webp_vp8l, "VP8L")],
)
def test_webp_size_is_read_for_every_container_variant(
    builder: Callable[[int, int], bytes], label: str
) -> None:
    assert read_image_info(builder(1024, 768)) == ImageInfo("image/webp", 1024, 768), (
        label
    )


def test_webp_edges() -> None:
    assert read_image_info(webp_vp8x(1, 1)) == ImageInfo("image/webp", 1, 1)
    assert read_image_info(webp_vp8x(16384, 16384)) == ImageInfo(
        "image/webp", 16384, 16384
    )
    assert read_image_info(webp_vp8l(16384, 1)) == ImageInfo("image/webp", 16384, 1)
    # lossy frames keep only 14 bits of the 16-bit field (the top two are scaling)
    lossy = riff(
        b"VP8 ", b"\x30\x01\x00\x9d\x01\x2a" + struct.pack("<HH", 0xC000 | 99, 7)
    )
    assert read_image_info(lossy) == ImageInfo("image/webp", 99, 7)


def test_unreadable_webp_is_none() -> None:
    assert (
        read_image_info(riff(b"VP8 ", b"\x30\x01\x00\x00\x00\x00" + b"\x00" * 4))
        is None
    )
    assert read_image_info(riff(b"VP8L", b"\x00" + b"\x00" * 4)) is None  # no 0x2F
    assert read_image_info(riff(b"ANIM", b"\x00" * 16)) is None
    assert read_image_info(webp_vp8x(10, 10)[:25]) is None
    assert read_image_info(webp_vp8(10, 10)[:25]) is None
    assert read_image_info(webp_vp8l(10, 10)[:22]) is None
    assert read_image_info(b"RIFF\x00\x00\x00\x00WEBP") is None


def test_unreadable_png_and_other_formats_are_none() -> None:
    good = build_png(10, 10, (0, 0, 0))
    assert read_image_info(good[:23]) is None  # truncated IHDR
    assert read_image_info(PNG_SIGNATURE) is None
    assert read_image_info(good[:12] + b"IDAT" + good[16:]) is None  # not IHDR first
    zero = good[:16] + struct.pack(">II", 0, 5) + good[24:]
    assert read_image_info(zero) is None  # a zero dimension is not an image
    assert read_image_info(b"") is None
    assert read_image_info(b"GIF89a" + b"\x00" * 20) is None
    assert read_image_info(b"plain text, not an image at all") is None


# --------------------------------------------------------------------------
# nearest_aspect_ratio
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "ratio"),
    [
        ((1024, 1024), "1:1"),
        ((1080, 1350), "4:5"),
        ((1350, 1080), "5:4"),
        ((1920, 1080), "16:9"),
        ((1080, 1920), "9:16"),
        ((1200, 800), "3:2"),
        ((800, 1200), "2:3"),
        ((1000, 1000), "1:1"),
        ((1, 1), "1:1"),
        ((4096, 1), "21:9"),  # the widest supported ratio
        ((1, 4096), "9:16"),  # the tallest supported ratio
    ],
)
def test_the_closest_supported_aspect_ratio_is_chosen(
    size: tuple[int, int], ratio: str
) -> None:
    assert nearest_aspect_ratio(size) == ratio
    assert ratio in GEMINI_ASPECT_RATIOS


# --------------------------------------------------------------------------
# GeminiImage (HTTP mocked with httpx.MockTransport)
# --------------------------------------------------------------------------

API_KEY = "AIza-test-key-123"
MODEL = "gemini-test-image"
PNG_BYTES = build_png(16, 16, (200, 10, 10))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def image_reply(data: bytes = PNG_BYTES, **extra: Any) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Here is your image."},
                        {"inlineData": {"mimeType": "image/png", "data": b64(data)}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        **extra,
    }


def gemini(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model: str = MODEL,
) -> tuple[GeminiImage, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    return GeminiImage(API_KEY, model, client=client), seen


def reply(status: int = 200, payload: Any = None, **kwargs: Any) -> Callable[..., Any]:
    return lambda _request: httpx.Response(
        status, json=image_reply() if payload is None else payload, **kwargs
    )


def test_gemini_returns_the_decoded_image_and_sends_the_documented_request() -> None:
    provider, seen = gemini(reply())

    data = provider.generate("  A mountain at dawn ", (1080, 1350))

    assert data == PNG_BYTES
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://generativelanguage.googleapis.com/v1/models/{MODEL}:generateContent"
    )
    assert request.headers["x-goog-api-key"] == API_KEY
    assert "authorization" not in request.headers
    assert API_KEY not in str(request.url)  # the key is never in the URL
    assert json.loads(request.content) == {
        "contents": [{"parts": [{"text": "A mountain at dawn"}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "responseFormat": {"image": {"aspectRatio": "4:5"}},
        },
    }


@pytest.mark.parametrize(
    ("size", "ratio"),
    [((1920, 1080), "16:9"), ((1080, 1920), "9:16"), ((10, 10), "1:1")],
)
def test_gemini_asks_for_the_aspect_ratio_closest_to_the_size(
    size: tuple[int, int], ratio: str
) -> None:
    provider, seen = gemini(reply())

    provider.generate("x", size)

    body = json.loads(seen[0].content)
    assert body["generationConfig"]["responseFormat"]["image"]["aspectRatio"] == ratio


def test_gemini_skips_thought_images_and_takes_the_first_real_one() -> None:
    thought = build_png(8, 8, (1, 1, 1))
    final = build_png(9, 9, (2, 2, 2))
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"inlineData": {"data": b64(thought)}, "thought": True},
                        {"text": "thinking...", "thought": True},
                        {"inlineData": {"mimeType": "image/png", "data": b64(final)}},
                        {"inlineData": {"data": b64(PNG_BYTES)}},
                    ]
                }
            }
        ]
    }
    provider, _ = gemini(reply(payload=payload))

    assert provider.generate("x") == final


def test_gemini_accepts_the_snake_case_spelling_of_inline_data() -> None:
    payload = {
        "candidates": [
            {"content": {"parts": [{"inline_data": {"data": b64(PNG_BYTES)}}]}}
        ]
    }
    provider, _ = gemini(reply(payload=payload))

    assert provider.generate("x") == PNG_BYTES


def test_gemini_returns_a_jpeg_or_webp_when_the_model_does() -> None:
    for data in (jpeg(100, 50), webp_vp8x(100, 50)):
        provider, _ = gemini(reply(payload=image_reply(data)))
        assert provider.generate("x") == data


def test_gemini_uses_the_injected_base_url_and_strips_a_trailing_slash() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=image_reply())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    GeminiImage(
        API_KEY, MODEL, base_url="https://proxy.test/v9/", client=client
    ).generate("x")

    assert str(seen[0].url) == f"https://proxy.test/v9/models/{MODEL}:generateContent"


def test_gemini_without_an_injected_client_uses_httpx_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        return httpx.Response(200, json=image_reply())

    monkeypatch.setattr("app.providers.image.httpx.post", fake_post)

    assert GeminiImage(API_KEY, MODEL, timeout=7.0).generate("x") == PNG_BYTES
    assert calls[0]["timeout"] == 7.0
    assert calls[0]["headers"] == {"x-goog-api-key": API_KEY}


@pytest.mark.parametrize(
    ("api_key", "model", "text"),
    [
        (None, MODEL, "GEMINI_API_KEY"),
        ("", MODEL, "GEMINI_API_KEY"),
        (API_KEY, None, "IMAGE_MODEL is not set"),
        (API_KEY, "   ", "IMAGE_MODEL is not set"),
        (API_KEY, "models/x", "invalid characters"),
        (API_KEY, "x?key=1", "invalid characters"),
        (API_KEY, "x:generateContent", "invalid characters"),
        (API_KEY, "../x", "invalid characters"),
        (API_KEY, "-x", "invalid characters"),
    ],
)
def test_gemini_needs_a_key_and_a_safe_model_name(
    api_key: str | None, model: str | None, text: str
) -> None:
    with pytest.raises(ImageConfigError) as caught:
        GeminiImage(api_key, model)

    assert text in caught.value.message
    assert caught.value.code == "image_not_configured"
    assert caught.value.retriable is False


def test_a_model_name_is_trimmed() -> None:
    assert GeminiImage(API_KEY, f"  {MODEL}  ").model == MODEL


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_a_config_error(status: int) -> None:
    provider, _ = gemini(reply(status, {"error": {"message": f"bad {API_KEY}"}}))

    with pytest.raises(ImageConfigError) as caught:
        provider.generate("x")

    assert "API key" in caught.value.message
    assert API_KEY not in caught.value.message


def test_an_unknown_model_is_a_config_error() -> None:
    provider, _ = gemini(reply(404, {"error": {"message": "models/x not found"}}))

    with pytest.raises(ImageConfigError) as caught:
        provider.generate("x")

    assert "IMAGE_MODEL" in caught.value.message
    assert caught.value.retriable is False


@pytest.mark.parametrize(
    ("status", "retriable"),
    [(400, False), (402, False), (422, False), (429, True), (500, True), (503, True)],
)
def test_http_errors_are_retriable_only_when_trying_again_can_help(
    status: int, retriable: bool
) -> None:
    provider, _ = gemini(reply(status, {"error": {"message": "echo: secret prompt"}}))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("secret prompt")

    assert caught.value.retriable is retriable
    assert str(status) in caught.value.message
    assert "secret prompt" not in caught.value.message  # the body is never echoed


def test_a_network_failure_is_retriable_and_leaks_nothing() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach host with {API_KEY} and secret prompt")

    provider, _ = gemini(refuse)

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("secret prompt")

    assert caught.value.retriable is True
    assert "ConnectError" in caught.value.message
    assert API_KEY not in caught.value.message
    assert "secret prompt" not in caught.value.message
    assert caught.value.__suppress_context__ is True  # raised ``from None``


def test_a_timeout_is_retriable() -> None:
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    provider, _ = gemini(slow)

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert caught.value.retriable is True


@pytest.mark.parametrize(
    "body", [b"<html>oops</html>", b"", b"[1, 2]", b'"text"', b"null"]
)
def test_an_unreadable_reply_is_not_retriable(body: bytes) -> None:
    provider, _ = gemini(lambda _r: httpx.Response(200, content=body))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert caught.value.retriable is False
    assert "unreadable" in caught.value.message


@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": [{"content": {"parts": [{"text": "I cannot draw."}]}}]},
        {
            "candidates": [
                {"content": {"parts": [{"text": "no"}]}, "finishReason": "STOP"}
            ]
        },
        {"candidates": [{"content": {}}]},
        {"candidates": [{}]},
        {"candidates": []},
        {"candidates": "nope"},
        {},
        {"candidates": [{"content": {"parts": [{"inlineData": {}}, "text", 5]}}]},
    ],
)
def test_a_reply_without_an_image_can_be_retried(payload: dict[str, Any]) -> None:
    provider, _ = gemini(reply(payload=payload))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert caught.value.retriable is True
    assert caught.value.code == "image_provider_error"
    assert "no image" in caught.value.message


@pytest.mark.parametrize(
    "payload",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": [], "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
        {"candidates": [{"finishReason": "IMAGE_SAFETY"}]},
        {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]},
        {"candidates": [{"finishReason": "NO_IMAGE"}]},
    ],
)
def test_a_refusal_is_reported_as_such_and_is_not_retriable(
    payload: dict[str, Any],
) -> None:
    provider, _ = gemini(reply(payload=payload))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert caught.value.code == "image_refused"
    assert caught.value.retriable is False


def test_an_image_wins_over_a_finish_reason() -> None:
    payload = image_reply()
    payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
    provider, _ = gemini(reply(payload=payload))

    assert provider.generate("x") == PNG_BYTES


@pytest.mark.parametrize("data", ["***not base64***", "abc", 12345, "AAAA\n" + "A" * 3])
def test_invalid_base64_is_not_retriable(data: Any) -> None:
    payload = {"candidates": [{"content": {"parts": [{"inlineData": {"data": data}}]}}]}
    provider, _ = gemini(reply(payload=payload))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert caught.value.retriable is False
    assert "unreadable image" in caught.value.message


def test_line_wrapped_base64_is_accepted() -> None:
    wrapped = "\n".join(
        b64(PNG_BYTES)[i : i + 20] for i in range(0, len(b64(PNG_BYTES)), 20)
    )
    payload = {
        "candidates": [{"content": {"parts": [{"inlineData": {"data": wrapped}}]}}]
    }
    provider, _ = gemini(reply(payload=payload))

    assert provider.generate("x") == PNG_BYTES


def test_a_stray_character_in_the_base64_is_an_error_not_silently_dropped() -> None:
    clean = b64(PNG_BYTES)
    corrupt = clean[:20] + "*" + clean[20:]  # lenient decoding would drop the "*"
    payload = {
        "candidates": [{"content": {"parts": [{"inlineData": {"data": corrupt}}]}}]
    }
    provider, _ = gemini(reply(payload=payload))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert "unreadable image" in caught.value.message
    assert caught.value.retriable is False


def test_data_that_is_not_an_image_is_not_retriable() -> None:
    provider, _ = gemini(reply(payload=image_reply(b"%PDF-1.7 not an image")))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("x")

    assert caught.value.retriable is False
    assert "not a PNG, JPEG or WebP" in caught.value.message


def test_gemini_logs_a_summary_without_the_prompt_or_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _ = gemini(reply())

    with caplog.at_level(logging.DEBUG):
        provider.generate("a very secret prompt")

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "Image generated" in logged
    assert "a very secret prompt" not in logged
    assert API_KEY not in logged


def test_a_failure_logs_nothing_sensitive(caplog: pytest.LogCaptureFixture) -> None:
    provider, _ = gemini(reply(500, {"error": "boom"}))

    with caplog.at_level(logging.DEBUG), pytest.raises(ImageProviderError):
        provider.generate("a very secret prompt")

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "a very secret prompt" not in logged
    assert API_KEY not in logged


def test_gemini_validates_the_request_before_calling_the_network() -> None:
    provider, seen = gemini(reply())

    with pytest.raises(InvalidImageRequestError):
        provider.generate("", (10, 10))
    with pytest.raises(InvalidImageRequestError):
        provider.generate("x", (0, 10))

    assert seen == []


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


def test_the_registry_returns_the_shared_fake(env: Any) -> None:
    env(IMAGE_PROVIDER="fake")

    assert get_image_provider() is get_fake_image()


def test_the_registry_is_case_insensitive(env: Any) -> None:
    env(IMAGE_PROVIDER=" FAKE ")

    assert get_image_provider() is get_fake_image()


def test_the_registry_builds_gemini_from_the_settings(env: Any) -> None:
    env(IMAGE_PROVIDER="gemini", GEMINI_API_KEY=API_KEY, IMAGE_MODEL=MODEL)

    provider = get_image_provider()

    assert isinstance(provider, GeminiImage)
    assert provider.model == MODEL


@pytest.mark.parametrize(
    ("key", "model", "text"),
    [("", MODEL, "GEMINI_API_KEY"), (API_KEY, "", "IMAGE_MODEL")],
)
def test_gemini_without_key_or_model_in_settings_is_a_config_error(
    env: Any, key: str, model: str, text: str
) -> None:
    env(IMAGE_PROVIDER="gemini", GEMINI_API_KEY=key, IMAGE_MODEL=model)

    with pytest.raises(ImageConfigError) as caught:
        get_image_provider()

    assert text in caught.value.message


def test_an_unknown_provider_is_a_config_error(env: Any) -> None:
    env(IMAGE_PROVIDER="midjourney")

    with pytest.raises(ImageConfigError) as caught:
        get_image_provider()

    assert "midjourney" in caught.value.message


def test_the_image_model_setting_defaults_to_none_and_no_name_is_hard_coded() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).IMAGE_MODEL is None
    assert Settings(_env_file=None).IMAGE_PROVIDER == "fake"
