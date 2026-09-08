"""The web inbound door maps the browser's ``Accept-Language`` to the turn's locale."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from tai42_channel_web.routes import _inbound_locale


def _request(accept_language: str | None) -> Request:
    headers = []
    if accept_language is not None:
        headers.append((b"accept-language", accept_language.encode()))
    return Request({"type": "http", "headers": headers})


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("he-IL,he;q=0.9,en;q=0.8", "he-IL"),
        ("pt-br", "pt-BR"),
        ("en-US", "en-US"),
    ],
)
def test_maps_top_accept_language(header: str, expected: str) -> None:
    assert _inbound_locale(_request(header)) == expected


@pytest.mark.parametrize("header", [None, "", "*", "not a locale!"])
def test_missing_or_wildcard_or_malformed_drops_to_none(header: str | None) -> None:
    assert _inbound_locale(_request(header)) is None
