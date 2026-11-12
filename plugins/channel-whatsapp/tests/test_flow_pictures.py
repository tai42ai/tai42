"""Form pictures: a link or a stored reference embedded as base64, and the size budget of pictured forms."""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.interactions import (
    MEDIA_ROUTE_PREFIX,
    MediaNotFoundError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    StoredMedia,
)
from tai42_kit.net import MediaFetchError, UrlGuardError, url_guard
from tai42_kit.net.request_body import RequestBodyTooLargeError
from tai42_kit.net.url_guard import UrlGuardSettings

from tai42_channel_whatsapp.flow_pictures import (
    embed_display_pictures,
    embed_page_pictures,
    is_picture_link,
    require_pictured_data_fits,
)
from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow
from tai42_channel_whatsapp.flows_endpoint import echoed_screen_data_max_bytes
from tai42_channel_whatsapp.flows_resolve import image_slots, pages_without_picture_content

from .conftest import PICTURE_JPEG as JPEG
from .conftest import PICTURE_PNG as PNG
from .conftest import FakePictureFetch

_LINK = "https://pics.example/a.jpg"
# An http link with an unclosed IPv6 bracket in its host: ``urlsplit`` raises on it.
_UNCLOSED_BRACKET_LINK = "http://[bad/x.png"
# Links the fetch's own parser refuses (a NUL in the host, a trailing newline, a port that is not a number).
_LINKS_THE_FETCH_CANNOT_PARSE = [
    "https://ex\x00ample.com/a.png?sig=k1",
    "https://pics.example/a.png?sig=k1\n",
    "http://pics.example:abc/a.png?sig=k1",
]
# Not a link, yet ``urlsplit`` cannot parse it either.
_UNPARSABLE_DATA = "//[not-a-link"
_SCHEMA = {"type": "object", "properties": {"note": {"type": "string"}}}


def _page(*display: dict[str, Any], title: str = "Form") -> dict[str, Any]:
    return {"title": title, "fields": ["note"], "kind": "input", "display": list(display)}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _images(flow_json: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "Image":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(flow_json)
    return found


async def test_a_static_picture_link_is_embedded_in_the_flow(picture_fetch: FakePictureFetch):
    pages = [_page({"kind": "image", "src": _LINK, "alt": "A picture"})]
    embedded = await embed_page_pictures(pages)
    flow_json, _ = build_form_flow(_SCHEMA, embedded)
    assert [image["src"] for image in _images(flow_json)] == [_b64(JPEG)]
    # The caller's pages are not changed in place.
    assert pages[0]["display"][0]["src"] == _LINK


async def test_a_slotted_picture_link_rides_the_send_data(picture_fetch: FakePictureFetch):
    pages = [_page({"kind": "image", "slot": "preview", "src": _LINK})]
    embedded = await embed_page_pictures(pages)
    flow_json, _ = build_form_flow(_SCHEMA, embedded)
    data = build_flow_data(_SCHEMA, {}, {}, embedded)
    assert [image["src"] for image in _images(flow_json)] == ["${data.slot_preview}"]
    assert data["slot_preview"] == _b64(JPEG)


async def test_a_non_link_src_is_kept_and_nothing_is_fetched(picture_fetch: FakePictureFetch):
    pages = [
        _page({"kind": "image", "src": _b64(PNG)}, {"kind": "body", "text": "https://text.example/"}),
        _page({"kind": "image", "src": _UNPARSABLE_DATA}, {"kind": "body", "text": _UNCLOSED_BRACKET_LINK}),
    ]
    assert await embed_page_pictures(pages) == pages
    assert await embed_page_pictures(None) is None
    assert picture_fetch.calls == []


def test_a_link_is_an_http_or_https_url_in_any_case():
    assert is_picture_link("HTTP://pics.example/a.png")
    assert is_picture_link("https://pics.example/a.png")
    assert not is_picture_link("ftp://pics.example/a.png")
    assert not is_picture_link(_b64(JPEG))
    assert not is_picture_link(None)


def test_only_the_scheme_decides_a_link_so_text_that_cannot_be_parsed_is_judged_without_raising():
    assert is_picture_link(_UNCLOSED_BRACKET_LINK)
    assert not is_picture_link(_UNPARSABLE_DATA)


@pytest.mark.parametrize(
    "src",
    [
        "https://pics.example/a.png",
        " \x00https://pics.example/a.png",
        "ht\ttps://pics.example/a.png",
        "HTTPS:pics.example",
        "https",
        "mailto:a@example.com",
        "/9j/4AAQSkZJRg==",
        "",
    ],
)
def test_the_link_rule_agrees_with_urlsplit_wherever_urlsplit_parses(src: str):
    assert is_picture_link(src) is (urlsplit(src).scheme in ("http", "https"))


def _chain_text(exc: BaseException) -> str:
    text = ""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        text += f"{current!s}{current!r}"
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return text


@pytest.mark.parametrize("link", _LINKS_THE_FETCH_CANNOT_PARSE)
async def test_a_picture_link_that_cannot_be_parsed_is_refused_with_its_place_before_any_fetch(
    picture_fetch: FakePictureFetch, link: str
):
    pages = [
        _page({"kind": "image", "src": "https://pics.example/ok.jpg"}, title="One"),
        _page({"kind": "image", "src": _UNPARSABLE_DATA}, {"kind": "image", "src": link}, title="Two"),
    ]
    with pytest.raises(ChannelInputError) as caught:
        await embed_page_pictures(pages)
    assert str(caught.value) == "form picture link cannot be parsed (page 2, picture 2)"
    assert "sig=k1" not in _chain_text(caught.value)
    assert picture_fetch.calls == []


async def test_a_display_link_that_cannot_be_parsed_names_its_slot_and_a_text_slot_keeps_such_text(
    picture_fetch: FakePictureFetch,
):
    pages = [_page({"kind": "image", "slot": "preview"}, {"kind": "body", "slot": "caption"})]
    link = _LINKS_THE_FETCH_CANNOT_PARSE[2]
    assert await embed_display_pictures({"caption": link}, pages) == {"caption": link}
    with pytest.raises(ChannelInputError) as caught:
        await embed_display_pictures({"preview": link, "caption": "Text"}, pages)
    assert str(caught.value) == "form picture link cannot be parsed (display slot 'preview')"
    assert picture_fetch.calls == []


async def test_one_link_is_fetched_once_and_distinct_links_concurrently(picture_fetch: FakePictureFetch):
    links = [f"https://pics.example/{n}.jpg" for n in range(3)]
    pages = [
        _page(*({"kind": "image", "src": link} for link in links), title="One"),
        _page({"kind": "image", "src": links[0]}, title="Two"),
    ]
    embedded = await embed_page_pictures(pages)
    assert embedded is not None
    assert sorted(url for url, _ in picture_fetch.calls) == links
    assert picture_fetch.most_in_flight == 3
    assert embedded[1]["display"][0]["src"] == _b64(JPEG)


async def test_the_fetch_follows_redirects_within_the_picture_size(picture_fetch: FakePictureFetch):
    await embed_page_pictures([_page({"kind": "image", "src": _LINK})])
    assert picture_fetch.calls == [(_LINK, {"follow_redirects": True, "max_bytes": 300_000})]


_GUARD_TEXT = "SSRF guard blocked host 'pics.example': resolves to non-public address"


@pytest.mark.parametrize(
    ("fault", "error_class", "text"),
    [
        (
            UrlGuardError(_GUARD_TEXT),
            ChannelInputError,
            f"form picture link refused (page 2, picture 2): {_GUARD_TEXT}",
        ),
        (
            MediaFetchError(host="pics.example", status_code=404),
            ChannelInputError,
            "form picture could not be fetched (page 2, picture 2): "
            "media fetch from host 'pics.example' failed: status 404",
        ),
        (
            MediaFetchError(host="pics.example", status_code=503),
            ChannelDeliveryError,
            "form picture could not be fetched (page 2, picture 2): "
            "media fetch from host 'pics.example' failed: status 503",
        ),
        (
            MediaFetchError(host="pics.example", cause_class="ConnectTimeout"),
            ChannelDeliveryError,
            "form picture could not be fetched (page 2, picture 2): "
            "media fetch from host 'pics.example' failed: ConnectTimeout",
        ),
        (
            RequestBodyTooLargeError("media body exceeds the 300000-byte cap"),
            ChannelInputError,
            "form picture is larger than the 300000 bytes this channel embeds in a form "
            "(page 2, picture 2, host 'pics.example')",
        ),
    ],
)
async def test_each_fetch_fault_is_refused_with_its_place_and_host(
    picture_fetch: FakePictureFetch, fault: Exception, error_class: type[Exception], text: str
):
    picture_fetch.faults[_LINK] = fault
    pages = [
        _page({"kind": "image", "src": "https://pics.example/ok.jpg"}, title="One"),
        _page({"kind": "body", "text": "Look"}, {"kind": "image", "src": _b64(PNG)}, title="Two"),
    ]
    pages[1]["display"].append({"kind": "image", "src": _LINK})
    with pytest.raises(error_class) as caught:
        await embed_page_pictures(pages)
    assert str(caught.value) == text
    assert caught.value.__cause__ is fault
    # A fault a fresh attempt may get past is a retryable delivery failure.
    assert getattr(caught.value, "retryable", False) is (error_class is ChannelDeliveryError)
    assert _LINK not in str(caught.value)


@pytest.mark.parametrize("raw", [JPEG, PNG])
async def test_jpeg_and_png_are_embedded(picture_fetch: FakePictureFetch, raw: bytes):
    picture_fetch.served[_LINK] = raw
    embedded = await embed_page_pictures([_page({"kind": "image", "src": _LINK})])
    assert embedded is not None
    assert embedded[0]["display"][0]["src"] == _b64(raw)


@pytest.mark.parametrize(
    "raw",
    [b"GIF89a" + b"\x00" * 16, b"RIFF\x10\x00\x00\x00WEBPVP8 ", b"<!doctype html><html></html>"],
)
async def test_other_formats_are_refused_whatever_the_content_type(picture_fetch: FakePictureFetch, raw: bytes):
    picture_fetch.served[_LINK] = raw
    with pytest.raises(ChannelInputError) as caught:
        await embed_page_pictures([_page({"kind": "image", "src": _LINK})])
    assert str(caught.value) == (
        "form picture is neither JPEG nor PNG, the formats WhatsApp draws (page 1, picture 1, host 'pics.example')"
    )


async def test_a_display_update_embeds_only_links_on_image_slots(picture_fetch: FakePictureFetch):
    pages = [_page({"kind": "image", "slot": "preview"}, {"kind": "body", "slot": "caption"})]
    display = {"preview": _LINK, "caption": "https://text.example/"}
    assert await embed_display_pictures(display, pages) == {"preview": _b64(JPEG), "caption": "https://text.example/"}
    assert [url for url, _ in picture_fetch.calls] == [_LINK]
    assert await embed_display_pictures(None, pages) is None


async def test_a_display_update_fault_names_its_slot(picture_fetch: FakePictureFetch):
    picture_fetch.faults[_LINK] = MediaFetchError(host="pics.example", status_code=404)
    with pytest.raises(ChannelInputError) as caught:
        await embed_display_pictures({"preview": _LINK}, [_page({"kind": "image", "slot": "preview"})])
    assert str(caught.value) == (
        "form picture could not be fetched (display slot 'preview'): "
        "media fetch from host 'pics.example' failed: status 404"
    )


def test_the_page_readers_name_image_slots_and_strip_picture_content():
    pages = [
        _page(
            {"kind": "image", "slot": "preview", "src": "c2xvdA=="},
            {"kind": "image", "src": "c3RhdGlj"},
            {"kind": "body", "slot": "caption", "text": "Hi"},
        )
    ]
    assert image_slots(pages) == {"preview"}
    stripped = pages_without_picture_content(pages)
    assert stripped is not None
    assert stripped[0]["display"] == [
        {"kind": "image", "slot": "preview"},
        {"kind": "image"},
        {"kind": "body", "slot": "caption", "text": "Hi"},
    ]
    assert pages[0]["display"][1]["src"] == "c3RhdGlj"
    assert pages_without_picture_content(None) is None


def _data_of_size(size: int) -> dict[str, str]:
    data = {"slot_preview": ""}
    data["slot_preview"] = "A" * (size - len(json.dumps(data, separators=(",", ":"))))
    return data


_PICTURE_SLOT_PAGES = [_page({"kind": "image", "slot": "preview"})]


@pytest.mark.parametrize(("endpoint", "budget"), [(False, 1_000_000), (True, echoed_screen_data_max_bytes())])
def test_picture_slot_data_at_the_budget_fits_and_one_byte_more_is_refused(endpoint: bool, budget: int):
    require_pictured_data_fits(_PICTURE_SLOT_PAGES, _data_of_size(budget), endpoint=endpoint)
    with pytest.raises(ChannelInputError) as caught:
        require_pictured_data_fits(_PICTURE_SLOT_PAGES, _data_of_size(budget + 1), endpoint=endpoint)
    assert str(caught.value) == (
        f"form data is {budget + 1} bytes, over the {budget} bytes this channel sends in a form with "
        "picture slots; slotted pictures ride this data and count toward it"
    )


@pytest.mark.parametrize(
    "pages",
    [
        None,
        [_page({"kind": "body", "slot": "caption", "text": "Hi"})],
        [_page({"kind": "image", "src": "c3RhdGlj"})],
    ],
    ids=["no-pages", "text-slot-only", "unslotted-image-only"],
)
@pytest.mark.parametrize("endpoint", [False, True])
def test_data_of_a_form_without_picture_slots_is_not_measured(pages: list[dict[str, Any]] | None, endpoint: bool):
    require_pictured_data_fits(pages, _data_of_size(2_000_000), endpoint=endpoint)


# --- one total time limit on each picture fetch ---

_SLOW_LINK = "https://pictures.example/a/slow.jpg?sig=k1"
# Long enough that a fetch with no total limit fails the test here instead of hanging the suite.
_TEST_BOUND = 3.0


def _limit_text(limit: float, where: str, host: str = "pictures.example") -> str:
    return f"form picture was not fetched within {limit} seconds ({where}, host {host!r})"


async def test_a_picture_fetch_past_the_limit_is_a_retryable_delivery_failure(
    picture_fetch: FakePictureFetch, picture_fetch_limit: float
):
    picture_fetch.stalled.add(_SLOW_LINK)
    async with asyncio.timeout(_TEST_BOUND):
        with pytest.raises(ChannelDeliveryError) as caught:
            await embed_page_pictures([_page({"kind": "image", "src": _SLOW_LINK})])
    assert str(caught.value) == _limit_text(picture_fetch_limit, "page 1, picture 1")
    assert caught.value.retryable is True
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert "/a/slow.jpg" not in str(caught.value)
    assert "sig=k1" not in str(caught.value)
    assert picture_fetch.cancelled == [_SLOW_LINK]


async def test_a_display_update_picture_past_the_limit_names_its_slot(
    picture_fetch: FakePictureFetch, picture_fetch_limit: float
):
    picture_fetch.stalled.add(_SLOW_LINK)
    async with asyncio.timeout(_TEST_BOUND):
        with pytest.raises(ChannelDeliveryError) as caught:
            await embed_display_pictures({"preview": _SLOW_LINK}, [_page({"kind": "image", "slot": "preview"})])
    assert str(caught.value) == _limit_text(picture_fetch_limit, "display slot 'preview'")
    assert caught.value.retryable is True


class _DripHandler(http.server.BaseHTTPRequestHandler):
    # Answers 200 with a large declared body, then sends one byte per interval until the client hangs up.
    server: _DripServer

    def do_GET(self) -> None:  # http.server dispatch name
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        try:
            self.wfile.write(PNG[:8])
            for _ in range(400):
                time.sleep(0.05)
                self.wfile.write(b"x")
                self.wfile.flush()
        except OSError:
            self.server.client_closed.set()

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class _DripServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _DripHandler)
        self.client_closed = threading.Event()


@pytest.fixture
def drip_server() -> Iterator[_DripServer]:
    server = _DripServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("guard", [UrlGuardSettings(enabled=False), UrlGuardSettings(allow_cidrs=["127.0.0.0/8"])])
async def test_a_dripping_host_is_cut_off_at_the_limit_through_the_real_fetch(
    monkeypatch: pytest.MonkeyPatch,
    picture_fetch_limit: float,
    drip_server: _DripServer,
    guard: UrlGuardSettings,
):
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: guard)
    link = f"http://127.0.0.1:{drip_server.server_address[1]}/pic.png"
    start = time.monotonic()
    async with asyncio.timeout(_TEST_BOUND):
        with pytest.raises(ChannelDeliveryError) as caught:
            await embed_page_pictures([_page({"kind": "image", "src": link})])
    assert time.monotonic() - start < picture_fetch_limit + 1.0
    assert str(caught.value) == _limit_text(picture_fetch_limit, "page 1, picture 1", host="127.0.0.1")
    assert await asyncio.to_thread(drip_server.client_closed.wait, 2.0)


async def test_stalled_links_on_one_form_end_within_one_limit(
    picture_fetch: FakePictureFetch, picture_fetch_limit: float
):
    links = [f"https://pictures.example/{n}.jpg" for n in range(3)]
    picture_fetch.stalled.update(links)
    start = time.monotonic()
    async with asyncio.timeout(_TEST_BOUND):
        with pytest.raises(ChannelDeliveryError):
            await embed_page_pictures([_page(*({"kind": "image", "src": link} for link in links))])
    assert time.monotonic() - start < 2 * picture_fetch_limit
    assert sorted(picture_fetch.cancelled) == links
    assert picture_fetch.in_flight == 0


async def test_the_failure_raised_is_the_first_in_form_order_not_the_first_in_time(
    picture_fetch: FakePictureFetch, picture_fetch_limit: float
):
    slow, quick = "https://pictures.example/slow.jpg", "https://pictures.example/quick.jpg"
    picture_fetch.stalled.add(slow)
    picture_fetch.faults[quick] = MediaFetchError(host="pictures.example", status_code=404)
    async with asyncio.timeout(_TEST_BOUND):
        with pytest.raises(ChannelDeliveryError) as caught:
            await embed_page_pictures([_page({"kind": "image", "src": slow}, {"kind": "image", "src": quick})])
    assert str(caught.value) == _limit_text(picture_fetch_limit, "page 1, picture 1")


async def test_a_fetch_within_the_limit_embeds_the_picture(picture_fetch: FakePictureFetch, picture_fetch_limit: float):
    embedded = await embed_page_pictures([_page({"kind": "image", "src": _LINK, "alt": "A picture"})])
    assert embedded == [_page({"kind": "image", "src": _b64(JPEG), "alt": "A picture"})]


async def test_an_outer_cancellation_is_not_rewritten_as_the_limit(picture_fetch: FakePictureFetch):
    picture_fetch.stalled.add(_SLOW_LINK)
    task = asyncio.create_task(embed_page_pictures([_page({"kind": "image", "src": _SLOW_LINK})]))
    async with asyncio.timeout(_TEST_BOUND):
        while not picture_fetch.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert picture_fetch.cancelled == [_SLOW_LINK]


async def test_a_single_network_step_timeout_keeps_its_own_text(
    picture_fetch: FakePictureFetch, picture_fetch_limit: float
):
    picture_fetch.faults[_LINK] = MediaFetchError(host="pics.example", cause_class="ReadTimeout")
    with pytest.raises(ChannelDeliveryError) as caught:
        await embed_page_pictures([_page({"kind": "image", "src": _LINK})])
    assert str(caught.value) == (
        "form picture could not be fetched (page 1, picture 1): "
        "media fetch from host 'pics.example' failed: ReadTimeout"
    )


# A picture given as a stored-media reference: read through the platform's media facet.

_STORED_ID = "S" * 43
_STORED = f"{MEDIA_ROUTE_PREFIX}{_STORED_ID}"


async def test_a_static_stored_picture_is_read_through_the_media_facet_and_embedded(
    stub_app, picture_fetch: FakePictureFetch
):
    stub_app.media.read_result = StoredMedia(mime="image/png", data=PNG)
    embedded = await embed_page_pictures([_page({"kind": "image", "src": _STORED, "alt": "A picture"})])
    flow_json, _ = build_form_flow(_SCHEMA, embedded)
    assert [image["src"] for image in _images(flow_json)] == [_b64(PNG)]
    assert stub_app.media.read_calls == [(_STORED_ID, 300_000)]
    assert picture_fetch.calls == []


async def test_a_slotted_stored_picture_rides_the_send_data(stub_app, picture_fetch: FakePictureFetch):
    stub_app.media.read_result = StoredMedia(mime="image/jpeg", data=JPEG)
    embedded = await embed_page_pictures([_page({"kind": "image", "slot": "preview", "src": _STORED})])
    assert build_flow_data(_SCHEMA, {}, {}, embedded)["slot_preview"] == _b64(JPEG)


async def test_an_absolute_served_reference_is_a_link_and_is_fetched(stub_app, picture_fetch: FakePictureFetch):
    absolute = f"https://cb.example{_STORED}"
    embedded = await embed_page_pictures([_page({"kind": "image", "src": absolute})])
    assert embedded is not None
    assert embedded[0]["display"][0]["src"] == _b64(JPEG)
    assert [url for url, _ in picture_fetch.calls] == [absolute]
    assert stub_app.media.read_calls == []


async def test_one_stored_picture_is_read_once_beside_a_link(stub_app, picture_fetch: FakePictureFetch):
    stub_app.media.read_result = StoredMedia(mime="image/png", data=PNG)
    pages = [
        _page({"kind": "image", "src": _STORED}, {"kind": "image", "src": _LINK}),
        _page({"kind": "image", "src": _STORED}),
    ]
    embedded = await embed_page_pictures(pages)
    assert embedded is not None
    assert [block["src"] for page in embedded for block in page["display"]] == [_b64(PNG), _b64(JPEG), _b64(PNG)]
    assert stub_app.media.read_calls == [(_STORED_ID, 300_000)]
    assert [url for url, _ in picture_fetch.calls] == [_LINK]


@pytest.mark.parametrize(
    ("fault", "raised", "text"),
    [
        (
            MediaNotFoundError("stored media not found"),
            ChannelInputError,
            "stored form picture was not found (page 1, picture 1): it is unknown, has expired, "
            "or is not attached to a message yet",
        ),
        (
            MediaStoreUnavailableError("media store unavailable"),
            ChannelDeliveryError,
            "stored form picture could not be read (page 1, picture 1): the media store is unavailable",
        ),
        (
            MediaTooLargeError("stored media is 300001 bytes, over the 300000-byte limit"),
            ChannelInputError,
            "form picture is larger than the 300000 bytes this channel embeds in a form (page 1, picture 1, "
            "stored picture)",
        ),
    ],
)
async def test_each_stored_picture_fault_is_refused_with_its_place_and_never_its_id(
    stub_app, fault: Exception, raised: type[Exception], text: str
):
    stub_app.media.read_error = fault
    with pytest.raises(raised) as caught:
        await embed_page_pictures([_page({"kind": "image", "src": _STORED})])
    assert type(caught.value) is raised
    assert str(caught.value) == text
    assert caught.value.__cause__ is fault
    assert _STORED_ID not in str(caught.value)


@pytest.mark.parametrize("raw", [b"GIF89a" + b"\x00" * 16, b"RIFF\x10\x00\x00\x00WEBPVP8 "])
async def test_stored_bytes_in_another_format_are_refused_whatever_the_stored_mime(stub_app, raw: bytes):
    stub_app.media.read_result = StoredMedia(mime="image/png", data=raw)
    with pytest.raises(ChannelInputError) as caught:
        await embed_page_pictures([_page({"kind": "image", "src": _STORED})])
    assert str(caught.value) == (
        "form picture is neither JPEG nor PNG, the formats WhatsApp draws (page 1, picture 1, stored picture)"
    )


async def test_a_display_update_reads_a_stored_picture_on_an_image_slot_only(stub_app, picture_fetch: FakePictureFetch):
    stub_app.media.read_result = StoredMedia(mime="image/png", data=PNG)
    pages = [_page({"kind": "image", "slot": "preview"}, {"kind": "body", "slot": "caption"})]
    assert await embed_display_pictures({"preview": _STORED, "caption": _STORED}, pages) == {
        "preview": _b64(PNG),
        "caption": _STORED,
    }
    assert stub_app.media.read_calls == [(_STORED_ID, 300_000)]


async def test_a_display_update_stored_picture_fault_names_its_slot(stub_app):
    stub_app.media.read_error = MediaNotFoundError("stored media not found")
    with pytest.raises(ChannelInputError) as caught:
        await embed_display_pictures({"preview": _STORED}, [_page({"kind": "image", "slot": "preview"})])
    assert str(caught.value) == (
        "stored form picture was not found (display slot 'preview'): it is unknown, has expired, "
        "or is not attached to a message yet"
    )
