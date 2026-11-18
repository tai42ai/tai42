"""Tests for the one rule of which picture sources Slack can load in an ``image`` block."""

from __future__ import annotations

import pytest

from tai42_channel_slack.image_source import is_loadable_image_url


@pytest.mark.parametrize("url", ["https://cdn.example/x.png", "http://cdn.example:8080/x.png"])
def test_an_absolute_http_url_with_a_host_is_loadable(url: str):
    assert is_loadable_image_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "/api/interactions/media/" + "S" * 43,
        "data:image/png;base64,AAAA",
        "ftp://cdn.example/x.png",
        "https://@/x.png",
        "http://:80/x.png",
        "http://[::1/x.png",
    ],
)
def test_a_source_without_an_http_scheme_and_a_host_is_not_loadable(url: str):
    assert not is_loadable_image_url(url)
