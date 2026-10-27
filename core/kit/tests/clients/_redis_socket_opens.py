"""Counting the socket opens of Redis connections."""

from __future__ import annotations

from typing import Any

import pytest
import redis.connection as sync_connection_mod

from tai42_kit.clients.impl import redis_connections as connections_mod


class ConnectCounter:
    def __init__(self) -> None:
        self.calls = 0


def count_async_connects(monkeypatch: pytest.MonkeyPatch) -> ConnectCounter:
    counter = ConnectCounter()
    original = connections_mod._TcpConnection._connect

    async def _connect(self: Any) -> None:
        counter.calls += 1
        await original(self)

    monkeypatch.setattr(connections_mod._TcpConnection, "_connect", _connect)
    return counter


def count_sync_connects(monkeypatch: pytest.MonkeyPatch) -> ConnectCounter:
    counter = ConnectCounter()
    original = sync_connection_mod.Connection._connect

    def _connect(self: Any) -> Any:
        counter.calls += 1
        return original(self)

    monkeypatch.setattr(sync_connection_mod.Connection, "_connect", _connect)
    return counter
