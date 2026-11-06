"""Harness self-tests for the reload gate's envelope predicate (:func:`_is_reloading`).

A worker holding its reload gate answers the retriable ``503 reloading`` envelope. The
admin and tool paths read it from the buffered body; an MCP initialize handshake the
gate rejects is raised by the fastmcp client as ``httpx.HTTPStatusError`` from
``__aenter__`` with its streaming body never read, so the session re-open recognises
the envelope on the RAISED response by the gate's ``Retry-After`` header. Any other
status is not the reload gate."""

from __future__ import annotations

import httpx
import pytest

from tai42_e2e.httpapi import _is_reloading

pytestmark = pytest.mark.needs("no-stack")


def _reloading_buffered() -> httpx.Response:
    """The reload gate's 503 as a client that READ the body sees it (the buffered
    admin/tool path): the ``reloading`` envelope plus the ``Retry-After`` header."""
    return httpx.Response(503, json={"error": "reloading — retry", "reloading": True}, headers={"Retry-After": "5"})


def _reloading_streaming() -> httpx.Response:
    """The reload gate's 503 as the raised MCP initialize exposes it: a streaming
    response whose body was never read (``raise_for_status`` fired first, the stream
    then closed), so only status + headers are readable."""
    return httpx.Response(503, headers={"Retry-After": "5"}, stream=httpx.ByteStream(b'{"reloading": true}'))


def _initializing_streaming() -> httpx.Response:
    """A NON-reloading 503 whose body is likewise unread — the ``Initializing...``
    503 an app answers before its inner MCP app is active. It carries NO ``Retry-After``,
    the reload gate's exclusive signature, so it must NOT be read as reloading."""
    return httpx.Response(503, stream=httpx.ByteStream(b'{"detail": "Initializing..."}'))


def test_is_reloading_buffered_envelope() -> None:
    assert _is_reloading(_reloading_buffered()) is True


def test_is_reloading_streaming_unread_envelope() -> None:
    # The initialize-raise case: the body is unrecoverable (ResponseNotRead), so the
    # gate's Retry-After header identifies the envelope.
    resp = _reloading_streaming()
    with pytest.raises(httpx.ResponseNotRead):
        resp.json()
    assert _is_reloading(resp) is True


def test_is_reloading_rejects_non_reloading_503() -> None:
    # A 503 with a readable body that is not the reloading envelope.
    assert _is_reloading(httpx.Response(503, json={"error": "Service Unavailable"})) is False
    # A 503 whose body is unread and which carries no Retry-After (the Initializing 503).
    assert _is_reloading(_initializing_streaming()) is False


def test_is_reloading_rejects_non_503() -> None:
    assert _is_reloading(httpx.Response(200, json={"data": 1})) is False
    # A Retry-After on a non-503 (a 429 rate-limit) is not the reload gate.
    assert _is_reloading(httpx.Response(429, headers={"Retry-After": "5"})) is False
