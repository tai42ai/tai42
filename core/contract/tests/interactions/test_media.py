"""Validator tests for ``MediaItem`` (its url discipline, host rules and caps),
``served_media_id``, and the ``InteractionRequest.media`` list rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tai42_contract.interactions.models import (
    MEDIA_CAPTION_MAX_CHARS,
    MEDIA_DATA_URI_MAX_CHARS,
    MEDIA_MAX_ITEMS,
    MEDIA_TOTAL_URI_CHARS,
    MEDIA_URL_MAX_CHARS,
    AnswerFormat,
    InteractionRequest,
    MediaItem,
    MediaKind,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _interaction(**overrides: Any) -> InteractionRequest:
    base: dict[str, Any] = {
        "interaction_id": "i1",
        "group_id": "g1",
        "question": "?",
        "reply_to": "ch",
        "created_at": _now(),
        "timeout_at": _now(),
    }
    base.update(overrides)
    return InteractionRequest(**base)


# === interactions/models.py — MediaItem + InteractionRequest.media ==========


_DATA_IMAGE = "data:image/png;base64,iVBORw0KGgo="


# -- MediaItem valid forms ---------------------------------------------------


def test_media_image_https_valid():
    item = MediaItem(kind=MediaKind.IMAGE, url="https://host/x.png")
    assert item.kind is MediaKind.IMAGE
    assert item.caption is None


def test_media_image_data_uri_valid():
    item = MediaItem(kind=MediaKind.IMAGE, url=_DATA_IMAGE)
    assert item.url == _DATA_IMAGE


def test_media_link_https_valid():
    item = MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Open")
    assert item.caption == "Open"


def test_media_link_http_valid():
    # http is valid for LINKS (anchors are not governed by img-src).
    item = MediaItem(kind=MediaKind.LINK, url="http://host/path")
    assert item.url == "http://host/path"


def test_media_caption_present_and_absent_valid():
    assert MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png", caption="alt").caption == "alt"
    assert MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png").caption is None


def test_media_request_mixed_eight_items_valid():
    items: list[dict[str, Any]] = []
    for i in range(MEDIA_MAX_ITEMS):
        if i % 2 == 0:
            items.append({"kind": "image", "url": f"https://host/{i}.png"})
        else:
            items.append({"kind": "link", "url": f"https://host/link/{i}", "caption": f"l{i}"})
    req = _interaction(media=items)
    assert req.media is not None
    assert len(req.media) == MEDIA_MAX_ITEMS


def test_media_dict_items_coerce_to_mediaitem():
    req = _interaction(media=[{"kind": "image", "url": "https://h/x.png", "caption": "c"}])
    assert req.media is not None
    assert isinstance(req.media[0], MediaItem)
    assert req.media[0].kind is MediaKind.IMAGE


def test_media_image_stored_reference_valid():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    url = MEDIA_ROUTE_PREFIX + secrets.token_urlsafe(32)
    item = MediaItem(kind=MediaKind.IMAGE, url=url)
    assert item.url == url


def test_media_image_absolute_served_https_valid():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    # An absolute served reference a channel send mints from an https public base url.
    url = "https://box.example" + MEDIA_ROUTE_PREFIX + secrets.token_urlsafe(32)
    assert MediaItem(kind=MediaKind.IMAGE, url=url).url == url


def test_media_image_absolute_served_http_localhost_valid():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    # http is allowed for a served reference: the base comes from public_base_url,
    # whose validator restricts http to localhost/127.0.0.1.
    url = "http://127.0.0.1:8000" + MEDIA_ROUTE_PREFIX + secrets.token_urlsafe(32)
    assert MediaItem(kind=MediaKind.IMAGE, url=url).url == url


def test_served_media_id_extracts_from_both_forms_and_rejects_the_rest():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX, served_media_id

    media_id = secrets.token_urlsafe(32)
    # Both served forms yield the id: the same-origin relative reference an inbox ask stores,
    # and the absolute reference a channel send mints from public_base_url.
    assert served_media_id(MEDIA_ROUTE_PREFIX + media_id) == media_id
    assert served_media_id("https://box.example" + MEDIA_ROUTE_PREFIX + media_id) == media_id
    # A non-served image or a link carries no stored id.
    assert served_media_id("https://cdn.example/p.png") is None
    assert served_media_id("https://docs.example/p") is None
    # Parse-robustness: the prefix is only located as the path prefix right after the origin —
    # a prefix buried in a query or fragment is not mistaken for a served id.
    assert served_media_id(f"https://cdn.example/x?next={MEDIA_ROUTE_PREFIX}{media_id}") is None
    assert served_media_id(f"https://cdn.example/x#{MEDIA_ROUTE_PREFIX}{media_id}") is None


def test_media_image_absolute_served_query_or_extra_segment_invalid():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    media_id = secrets.token_urlsafe(32)
    # http is admitted ONLY for the exact served form; a query, a fragment, or an extra
    # path segment past the id breaks that form and — http not being a valid general
    # image scheme — leaves no accepting branch. (An https base with a query would still
    # be a valid general https image, so the exactness only bites on the http form.)
    base = "http://127.0.0.1:8000" + MEDIA_ROUTE_PREFIX
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=base + media_id + "?x=1")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=base + media_id + "#frag")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=base + media_id + "/extra")


def test_media_image_absolute_http_non_served_invalid():
    # http is admitted only for the served-reference path; a plain http image url is
    # not that form and stays invalid.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="http://box.example/photo.png")


def test_media_image_absolute_served_http_loopback_hosts_valid():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    # http is admitted for a served reference on any loopback host: the base is
    # minted from public_base_url, whose validator restricts http to those hosts.
    for base in ("http://localhost", "http://127.0.0.1:8000", "http://[::1]:8000"):
        url = base + MEDIA_ROUTE_PREFIX + secrets.token_urlsafe(32)
        assert MediaItem(kind=MediaKind.IMAGE, url=url).url == url


def test_media_image_absolute_served_http_non_loopback_rejected():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    # http is admitted only for a loopback served base; a public or private-LAN host
    # over http is not a valid served reference and has no other accepting branch
    # (remote images are https-only).
    media_id = secrets.token_urlsafe(32)
    for base in ("http://evil.example", "http://10.0.0.5:9000"):
        with pytest.raises(ValueError, match="absolute https URL or a data:image"):
            MediaItem(kind=MediaKind.IMAGE, url=base + MEDIA_ROUTE_PREFIX + media_id)


# -- MediaItem rejected forms ------------------------------------------------


def test_media_image_rejects_javascript_url():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="javascript:alert(1)")


def test_media_link_rejects_javascript_url():
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="javascript:alert(1)")


def test_media_image_rejects_http_url():
    # Remote images are https-only (the inbox CSP img-src blocks http:).
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="http://host/x.png")


def test_media_link_rejects_data_uri():
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url=_DATA_IMAGE)


def test_media_image_rejects_non_image_data_uri():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="data:text/html,<h1>x</h1>")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="data:application/pdf;base64,AAAA")


def test_media_rejects_relative_path_for_both_kinds():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="/api/storage/resources/1/content")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="/api/storage/resources/1/content")


def test_media_image_rejects_stored_reference_malformed_id():
    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    # A served reference is valid only with a well-formed 43-char id and nothing after
    # it; a short id, a trailing segment, or a bad charset falls through to the raise.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + "short")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + "a" * 43 + "/extra")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=MEDIA_ROUTE_PREFIX + "!" * 43)


def test_media_link_rejects_stored_reference():
    import secrets

    from tai42_contract.interactions.models import MEDIA_ROUTE_PREFIX

    # The served-media reference is an image-only branch; a link stays absolute http(s).
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url=MEDIA_ROUTE_PREFIX + secrets.token_urlsafe(32))


def test_media_rejects_empty_and_whitespace_url():
    with pytest.raises(ValueError, match="non-blank"):
        MediaItem(kind=MediaKind.IMAGE, url="")
    with pytest.raises(ValueError, match="non-blank"):
        MediaItem(kind=MediaKind.LINK, url="   ")


def test_media_rejects_url_with_empty_netloc():
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://")


def test_media_rejects_userinfo_in_url():
    # ``https://trusted.com@evil.com`` resolves to host evil.com while displaying a
    # trusted-looking authority — an embedded ``user@`` credential form is rejected
    # as a spoofing vector, for both kinds.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://trusted.com@evil.com/x.png")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://trusted.com@evil.com/p")


def test_media_rejects_hostless_userinfo_url():
    # ``https://user@`` has a non-empty netloc but no host; it is rejected (the
    # check requires a real hostname, not merely a non-empty authority).
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://user@")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://user:pass@")


def test_media_rejects_malformed_authority_with_rule_message():
    # An unterminated IPv6 literal makes urlsplit raise; the validator fails closed
    # with its own rule-naming message rather than leaking the parser's exception.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://[")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://[")


def test_media_in_range_port_accepted():
    # The maximum valid port (65535) parses; an explicit in-range port is kept.
    assert (
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example:65535/p.png").url == "https://cdn.example:65535/p.png"
    )
    assert MediaItem(kind=MediaKind.LINK, url="https://cdn.example:65535/p").url == "https://cdn.example:65535/p"


def test_media_rejects_out_of_range_port():
    # A port past 65535 is spec-invalid: ``urlsplit(...).port`` raises ValueError,
    # so the browser's URL parser would reject the replayed frame and drop the card.
    # The validator fails closed for both kinds.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example:65536/p.png")
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example:99999/p.png")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://cdn.example:65536/p")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://cdn.example:99999/p")


def test_media_portless_url_unaffected():
    # A portless URL still validates for both kinds — the port check only rejects
    # an explicit out-of-range port, never a URL with no port.
    assert MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/p.png").url == "https://cdn.example/p.png"
    assert MediaItem(kind=MediaKind.LINK, url="https://cdn.example/p").url == "https://cdn.example/p"


_BAD_HOSTS = [
    "999.999.999.999",  # IPv4 octet overflow
    "1.2.3.4.5",  # five-octet IPv4-lookalike
    "256.1.1.1",  # IPv4 octet out of range
    "4294967296",  # decimal IPv4 integer overflow
    "0x100000000",  # hex IPv4 integer overflow
    "0x",  # bare 0x prefix — the browser reads it as IPv4 zero
    "example.0x",  # bare 0x final label
    "ex%zz.example.com",  # percent-encoding in host
    "%00.com",  # percent-encoded NUL in host
    "user%40host.com",  # percent-encoded @ in host
    "host%9f.com",  # percent-encoded control byte in host
    "[fe80::1%eth0]",  # bracketed IPv6 with a WHATWG-forbidden zone id
]


@pytest.mark.parametrize("host", _BAD_HOSTS)
def test_media_rejects_ipv4_lookalike_and_percent_hosts(host: str):
    # Hosts the browser's new URL() rejects: IPv4-shorthand/overflow lookalikes and
    # any percent-encoding in the authority. A contract-valid media URL that the
    # widget then drops on replay is the bug; reject at send for both kinds.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url=f"https://{host}/x.png")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url=f"https://{host}/p")


_GOOD_HOSTS = [
    "example.com",  # normal domain
    "cdn.assets.example.com",  # multi-label domain
    "xn--p1ai",  # punycode single label
    "foo.xn--80akhbyknj4f",  # punycode final label
    "host",  # single-label host
    "example.com.",  # trailing-dot host
    "192.168.1.1",  # valid dotted-quad IPv4
    "[2001:db8::1]",  # bracketed IPv6
    "my-host.example-site.com",  # hyphenated labels
]


@pytest.mark.parametrize("host", _GOOD_HOSTS)
def test_media_accepts_valid_hosts(host: str):
    # The pinned accepted set: the grammar must stay wide enough for these.
    assert MediaItem(kind=MediaKind.IMAGE, url=f"https://{host}/x.png").url == f"https://{host}/x.png"
    assert MediaItem(kind=MediaKind.LINK, url=f"https://{host}/p").url == f"https://{host}/p"


def test_media_rejects_unicode_idn_host():
    # Unicode/IDN hosts are rejected — the grammar is ASCII-only; IDN callers supply
    # punycode.
    with pytest.raises(ValueError, match="absolute https URL or a data:image"):
        MediaItem(kind=MediaKind.IMAGE, url="https://münchen.example/x.png")
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        MediaItem(kind=MediaKind.LINK, url="https://münchen.example/p")


def test_media_rejects_interior_whitespace_and_control_chars():
    # urlsplit strips \t\r\n before parsing, so an embedded newline/tab would let
    # the validated string diverge from the stored one — reject it up front.
    with pytest.raises(ValueError, match="no whitespace or control characters"):
        MediaItem(kind=MediaKind.IMAGE, url="https://ho\nst/x.png")
    with pytest.raises(ValueError, match="no whitespace or control characters"):
        MediaItem(kind=MediaKind.LINK, url="https://ho st/x")
    # A bidi override inside the host survives urlsplit; reject it as a spoofing vector.
    with pytest.raises(ValueError, match="no whitespace or control characters"):
        MediaItem(kind=MediaKind.LINK, url="https://ev‮il.com/p")


def test_media_rejects_unknown_kind():
    # A dict item with an unknown kind is rejected when it coerces through MediaItem.
    with pytest.raises(ValueError, match="'link'"):
        _interaction(media=[{"kind": "carrier-pigeon", "url": "https://h/x"}])


def test_media_image_data_uri_over_cap_raises():
    url = "data:image/png;base64," + "A" * MEDIA_DATA_URI_MAX_CHARS
    with pytest.raises(ValueError, match=f"data: URI must be at most {MEDIA_DATA_URI_MAX_CHARS}"):
        MediaItem(kind=MediaKind.IMAGE, url=url)


def test_media_https_url_over_cap_raises():
    url = "https://host/" + "a" * MEDIA_URL_MAX_CHARS
    with pytest.raises(ValueError, match=f"url must be at most {MEDIA_URL_MAX_CHARS}"):
        MediaItem(kind=MediaKind.IMAGE, url=url)


def test_media_link_url_over_cap_raises():
    url = "https://host/" + "a" * MEDIA_URL_MAX_CHARS
    with pytest.raises(ValueError, match=f"link media url must be at most {MEDIA_URL_MAX_CHARS}"):
        MediaItem(kind=MediaKind.LINK, url=url)


def test_media_blank_caption_raises():
    with pytest.raises(ValueError, match="caption must be non-blank"):
        MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png", caption="   ")


def test_media_caption_over_cap_raises():
    with pytest.raises(ValueError, match=f"caption must be at most {MEDIA_CAPTION_MAX_CHARS}"):
        MediaItem(kind=MediaKind.IMAGE, url="https://h/x.png", caption="c" * (MEDIA_CAPTION_MAX_CHARS + 1))


def test_media_exact_cap_boundaries_accepted():
    # The caps are strict ``>``; a value of EXACTLY the cap is accepted (guards a
    # ``>`` -> ``>=`` regression).
    at_cap_url = "https://host/" + "a" * (MEDIA_URL_MAX_CHARS - len("https://host/"))
    assert len(at_cap_url) == MEDIA_URL_MAX_CHARS
    assert MediaItem(kind=MediaKind.IMAGE, url=at_cap_url).url == at_cap_url

    prefix = "data:image/png;base64,"
    at_cap_data = prefix + "A" * (MEDIA_DATA_URI_MAX_CHARS - len(prefix))
    assert len(at_cap_data) == MEDIA_DATA_URI_MAX_CHARS
    assert MediaItem(kind=MediaKind.IMAGE, url=at_cap_data).url == at_cap_data

    at_cap_caption = "c" * MEDIA_CAPTION_MAX_CHARS
    assert MediaItem(kind=MediaKind.LINK, url="https://h/x", caption=at_cap_caption).caption == at_cap_caption


# -- InteractionRequest.media list rules -------------------------------------


def test_media_empty_list_raises():
    with pytest.raises(ValueError, match="non-empty list"):
        _interaction(media=[])


def test_media_over_max_items_raises():
    items = [{"kind": "image", "url": f"https://host/{i}.png"} for i in range(MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(ValueError, match=f"at most {MEDIA_MAX_ITEMS} items"):
        _interaction(media=items)


def test_media_total_uri_budget_raises():
    # Each item is within MEDIA_DATA_URI_MAX_CHARS, but the summed url text
    # exceeds the per-question MEDIA_TOTAL_URI_CHARS budget.
    per_item = "data:image/png;base64," + "A" * 400_000
    assert len(per_item) <= MEDIA_DATA_URI_MAX_CHARS
    items = [{"kind": "image", "url": per_item} for _ in range(3)]
    assert sum(len(i["url"]) for i in items) > MEDIA_TOTAL_URI_CHARS
    with pytest.raises(ValueError, match=f"total url length must be at most {MEDIA_TOTAL_URI_CHARS}"):
        _interaction(media=items)


def test_media_total_uri_budget_at_cap_accepted():
    # The total budget is a strict ``>``; a list summing to EXACTLY
    # MEDIA_TOTAL_URI_CHARS is accepted (guards a ``>`` -> ``>=`` regression).
    prefix = "data:image/png;base64,"
    half = MEDIA_TOTAL_URI_CHARS // 2
    url_a = prefix + "A" * (half - len(prefix))
    url_b = prefix + "B" * (MEDIA_TOTAL_URI_CHARS - half - len(prefix))
    items = [{"kind": "image", "url": url_a}, {"kind": "image", "url": url_b}]
    assert sum(len(i["url"]) for i in items) == MEDIA_TOTAL_URI_CHARS
    req = _interaction(media=items)
    assert req.media is not None
    assert len(req.media) == 2


# -- round-trip + orthogonality ----------------------------------------------


def test_media_defaults_none_and_json_round_trips():
    # Omitting media yields None, and an explicit ``"media": null`` reloads as None.
    assert _interaction().media is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.media is None


def test_media_absent_field_loads_as_none():
    # A serialized request whose JSON omits the ``media`` key loads with media None:
    # a missing field reads as the None default, so a record lacking the key is valid.
    payload = json.loads(_interaction().model_dump_json())
    del payload["media"]
    assert "media" not in payload
    assert InteractionRequest.model_validate_json(json.dumps(payload)).media is None


def test_media_json_round_trips_exactly():
    req = _interaction(
        media=[
            {"kind": "image", "url": "https://h/x.png", "caption": "alt"},
            {"kind": "image", "url": _DATA_IMAGE},
            {"kind": "link", "url": "https://shop/p/1"},
        ]
    )
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.media == req.media
    assert restored.media is not None
    assert restored.media[1].caption is None


@pytest.mark.parametrize(
    ("answer_format", "format_payload"),
    [
        (AnswerFormat.TEXT, None),
        (AnswerFormat.CONFIRM, None),
        (AnswerFormat.SELECT, {"options": ["a", "b"]}),
        (AnswerFormat.FORM, {"schema": {"type": "object"}}),
        (AnswerFormat.EXTERNAL, {"url": "https://sign.example/abc"}),
    ],
)
def test_media_orthogonal_to_every_answer_format(answer_format: AnswerFormat, format_payload: Any):
    req = _interaction(
        answer_format=answer_format,
        format_payload=format_payload,
        media=[{"kind": "image", "url": "https://h/x.png"}],
        sensitive=True,
        channel="telegram",
        audience="user-1",
    )
    assert req.media is not None
    assert req.format_payload == format_payload
    assert req.sensitive is True
    assert req.channel == "telegram"
    assert req.audience == "user-1"
