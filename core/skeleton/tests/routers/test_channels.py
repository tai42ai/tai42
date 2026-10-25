"""Channels router: the authed catalog door lists the registered channels, each with its
declared identity hint, inside the ``{"data": ...}`` envelope."""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.channels import SendOutcome

from tai42_skeleton.routers import channels as router

from .._helpers import DeliverOnlyChannel


class _Chan(DeliverOnlyChannel):
    async def deliver(self, delivery) -> SendOutcome:
        return self.outcome(delivery, [])


@pytest.fixture
def registry():
    """The process app's channel registry, with ``tai42_app`` bound to that same
    app so the route resolves channels from it; cleared after the test."""
    from tai42_skeleton.app.instance import build_app

    app = build_app()
    tai42_app.bind(app)
    reg = app._channel_registry
    reg.reset()
    try:
        yield reg
    finally:
        reg.reset()


def _get_request() -> Request:
    scope = {"type": "http", "method": "GET", "path": "/api/channels", "query_string": b"", "headers": []}
    return Request(scope)


async def test_list_channels_empty(registry) -> None:
    resp = await router.list_channels(_get_request())
    assert resp.status_code == 200
    assert json.loads(bytes(resp.body)) == {"data": {"channels": []}}


class _HintedChan(_Chan):
    identity_hint = "The number clients text."


async def test_list_channels_returns_sorted_entries_with_their_hints(registry) -> None:
    tai42_app.channels.register("b", _Chan())
    tai42_app.channels.register("a", _HintedChan())
    resp = await router.list_channels(_get_request())
    assert json.loads(bytes(resp.body)) == {
        "data": {
            "channels": [
                {"name": "a", "identity_hint": "The number clients text."},
                {"name": "b", "identity_hint": None},
            ]
        }
    }
