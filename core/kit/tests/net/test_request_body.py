"""Tests for the shared bounded body reader: it streams a request's chunks counting
ACTUAL bytes, returns the joined body under the cap, and raises ``PayloadTooLarge``
the moment the running total crosses the cap (never a truncated shorter body)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tai42_kit.net.request_body import PayloadTooLarge, read_bounded_body


class _FakeRequest:
    """A minimal streamable request: replays the given chunks from ``stream()``,
    satisfying the reader's ``_StreamableRequest`` protocol without starlette."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


async def test_under_cap_returns_the_joined_bytes():
    request = _FakeRequest([b"abc", b"de", b"f"])
    assert await read_bounded_body(request, cap=100) == b"abcdef"


async def test_exactly_at_cap_is_ok():
    request = _FakeRequest([b"abcd", b"ef"])
    assert await read_bounded_body(request, cap=6) == b"abcdef"


async def test_over_cap_raises_payload_too_large():
    request = _FakeRequest([b"abcd", b"efg"])
    with pytest.raises(PayloadTooLarge):
        await read_bounded_body(request, cap=6)


async def test_the_cap_counts_actual_bytes_across_chunks_not_a_single_chunk():
    # No single chunk exceeds the cap, but their running total does — the reader
    # raises on the crossing chunk, never after joining an over-cap body.
    request = _FakeRequest([b"aaa", b"bbb", b"ccc"])
    with pytest.raises(PayloadTooLarge):
        await read_bounded_body(request, cap=7)


async def test_empty_body_returns_empty_bytes():
    request = _FakeRequest([])
    assert await read_bounded_body(request, cap=10) == b""
