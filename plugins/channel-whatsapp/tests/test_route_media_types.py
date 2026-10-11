"""The vendor doors publish the plain-text bodies they answer, not ``application/json``."""

from __future__ import annotations

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import _StubApp


def _route(stub_app: _StubApp, path: str):
    (route,) = [route for route in stub_app.http.routes if route.path == path]
    return route


def test_the_verification_get_publishes_text_plain(stub_app: _StubApp) -> None:
    assert _route(stub_app, "/inbound").success_media_types == {"GET": ("text/plain",)}


def test_the_flow_data_post_publishes_text_plain(stub_app: _StubApp) -> None:
    assert _route(stub_app, "/flow-data").success_media_types == {"POST": ("text/plain",)}
