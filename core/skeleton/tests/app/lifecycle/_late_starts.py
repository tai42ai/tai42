"""Doubles and drivers of the MCP server start tests: a server whose listing takes its time, and the
rebuild's listing driven as the epoch build runs it."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.app import instance

from ...connectors.credential_path import on_loop
from ._doubles import _cfg, _FakeMcpTool, _Mixin, _mixin


class _SlowListing:
    """A server whose listing takes ``seconds``, then lists one tool or raises ``error``."""

    def __init__(self, seconds: float, *, error: BaseException | None = None) -> None:
        self.seconds = seconds
        self.error = error
        self.cancelled = threading.Event()
        self.listed = threading.Event()

    @asynccontextmanager
    async def client_ctx(self, client_cls, *args, **kwargs):
        yield self

    async def list_tools(self):
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.listed.set()
        if self.error is not None:
            raise self.error
        return [_FakeMcpTool()]


class _SequencedListing(_SlowListing):
    """A server whose successive listings each take their own ``(seconds, error)``."""

    def __init__(self, *listings: tuple[float, BaseException | None]) -> None:
        super().__init__(0.0)
        self.listings = list(listings)

    async def list_tools(self):
        seconds, error = self.listings.pop(0)
        await asyncio.sleep(seconds)
        if error is not None:
            raise error
        return [_FakeMcpTool()]


class _CountingListing(_SlowListing):
    """Successive listings, each its own ``(seconds, error)``; counts the listings started and those live at once.

    It lists the tool ``t``, whose served name is the one the :class:`_Mixin` bind records, so a
    read of the listing a bind kept agrees with what the bind bound.
    """

    def __init__(self, *listings: tuple[float, BaseException | None]) -> None:
        super().__init__(0.0)
        self.listings = list(listings)
        self.started = 0
        self.live = 0
        self.max_live = 0

    async def list_tools(self):
        seconds, error = self.listings.pop(0)
        self.started += 1
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        try:
            await asyncio.sleep(seconds)
        finally:
            self.live -= 1
        if error is not None:
            raise error
        return [_FakeMcpTool("t")]


def _slow_mixin(monkeypatch: pytest.MonkeyPatch, server: _SlowListing, config: TaiMCPConfig | None = None) -> _Mixin:
    """A mixin whose MCP servers are ``server``, served as the process's built app, which owns the serving loop."""
    m = _mixin(config or _cfg("svc"))
    monkeypatch.setattr(m.clients, "client_ctx", server.client_ctx)
    monkeypatch.setattr(instance, "_app", m)
    return m


async def _listing_of_rebuild(m: _Mixin) -> list[tuple[TaiMCPConfig, Any]]:
    """The rebuild's listing as the epoch build runs it, each failure recorded; answers the successes."""
    successes, failures = await m._load_mcps()
    for config, exc in failures:
        m._record_failed_mcp(config, exc)
    return successes


def _rebuild_listing(m: _Mixin, loop: asyncio.AbstractEventLoop) -> list[tuple[TaiMCPConfig, Any]]:
    """The rebuild's listing (:func:`_listing_of_rebuild`), awaited on ``loop``."""
    return on_loop(loop, _listing_of_rebuild(m))


def _next_generation(m: _Mixin) -> None:
    """Make a fresh serving core over the same manifest the read target, as an epoch swap does."""
    manifest = m._manifest
    m._scaffold_core = m._build_serving_core()
    m._manifest = manifest


def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the late start outcome was not applied in time"
        time.sleep(0.01)
