"""Harness self-tests for the reload-gate envelope handling in the boot/restart
wait probes.

A worker still holding its boot/reload self-resync gate rejects the MCP initialize
handshake with the retriable ``reloading`` envelope (an auth stack's request path
answers it while the identity registry is mid-rebuild). The fastmcp client raises
``httpx.HTTPStatusError`` from ``__aenter__``, before a session exists, so the
tool-call ``retry_on_reloading`` path never sees it — the drain probe must recognise
that envelope on the RAISED response and keep polling, while any other status kills
the wait. These tests pin both the canonical envelope predicate (:func:`_is_reloading`,
including the unread-streaming-body case the initialize raise produces) and the probe
wrapper (:func:`_probe_tolerating_reloading`)."""

from __future__ import annotations

import httpx
import pytest

from tai42_e2e.httpapi import _is_reloading
from tai42_e2e.stack import _probe_tolerating_reloading

_URL = "http://127.0.0.1:1/mcp"


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


def _status_error(response: httpx.Response) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("status error", request=httpx.Request("POST", _URL), response=response)


async def test_probe_treats_reloading_envelope_as_not_ready() -> None:
    # Case: the client-open raises the reloading envelope → the wrapper returns None so
    # the enclosing wait keeps polling within its deadline.
    async def open_and_call() -> dict[str, int]:
        raise _status_error(_reloading_streaming())

    assert await _probe_tolerating_reloading(open_and_call) is None


async def test_probe_propagates_non_reloading_status_error() -> None:
    # Case: a non-reloading 503 (or any other status) is a real failure → re-raised.
    async def open_and_call() -> dict[str, int]:
        raise _status_error(_initializing_streaming())

    with pytest.raises(httpx.HTTPStatusError):
        await _probe_tolerating_reloading(open_and_call)


async def test_probe_returns_ready_result() -> None:
    # Case: the client-open succeeds → the wrapper returns the probe's own result.
    async def open_and_call() -> dict[str, int]:
        return {"pid": 123}

    assert await _probe_tolerating_reloading(open_and_call) == {"pid": 123}
