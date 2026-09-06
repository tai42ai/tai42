"""Harness self-test: a fresh session refuses to lease infra a leaked stack still holds.

The per-stack logical-DB isolation breaks down when a DB index is re-leased while
a LEAKED worker from an earlier run still consumes it (the exact fault that let an
orphaned backend answer a sibling stack's tool jobs). ``RedisAdmin.allocate_db``
therefore refuses an index that still carries a live foreign ``bus:presence`` key,
naming the pid, rather than flushing over it and leasing the DB under the orphan;
``ports.reserve_specific_port`` refuses a pinned port something already listens on.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tai42_e2e import ports, redisx


class _FakeRedis:
    """A stand-in Redis client whose scan/get replay a preset presence keyspace,
    shared across every ``redis.Redis(...)`` construction in one test."""

    def __init__(self, scan_keys: dict[str, str]) -> None:
        self._scan_keys = scan_keys
        self.flushed = False

    def scan_iter(self, match: str | None = None) -> Any:
        yield from self._scan_keys

    def get(self, key: str) -> str | None:
        return self._scan_keys.get(key)

    def flushdb(self) -> None:
        self.flushed = True

    def ping(self) -> None:
        pass

    def close(self) -> None:
        pass


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch, scan_keys: dict[str, str]) -> _FakeRedis:
    fake = _FakeRedis(scan_keys)
    monkeypatch.setattr(redisx.redis, "Redis", lambda *a, **k: fake)
    return fake


def test_allocate_db_refuses_a_db_with_a_live_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    presence = {"tai42_e2e_old:bus:presence:backend-0": '{"pid": 4242, "kind": "backend"}'}
    _install_fake_redis(monkeypatch, presence)
    monkeypatch.setattr(redisx, "_pid_alive", lambda pid: True)

    admin = redisx.RedisAdmin("127.0.0.1", 6379)
    with pytest.raises(RuntimeError, match=r"live worker.*pid 4242.*kind=backend"):
        admin.allocate_db()


def test_allocate_db_ignores_stale_keys_from_a_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    presence = {"tai42_e2e_old:bus:presence:serve-0": '{"pid": 99, "kind": "serve"}'}
    fake = _install_fake_redis(monkeypatch, presence)
    monkeypatch.setattr(redisx, "_pid_alive", lambda pid: False)

    admin = redisx.RedisAdmin("127.0.0.1", 6379)
    idx = admin.allocate_db()
    # A dead process's leftover keys are not an orphan: the index is flushed and leased.
    assert idx in redisx._STACK_DB_RANGE
    assert fake.flushed is True


def test_allocate_db_leases_a_clean_db(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_redis(monkeypatch, {})
    monkeypatch.setattr(redisx, "_pid_alive", lambda pid: True)

    admin = redisx.RedisAdmin("127.0.0.1", 6379)
    idx = admin.allocate_db()
    assert idx in redisx._STACK_DB_RANGE
    assert fake.flushed is True


def test_reserve_specific_port_refuses_a_busy_port() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            ports.reserve_specific_port(port)
    finally:
        sock.close()
        ports.release_port(port)


def test_pids_listening_yields_nothing_when_the_diagnostic_tools_are_absent(monkeypatch) -> None:
    """An absent ``lsof``/``ss`` never turns the busy-port refusal into a crash: the
    pid lookup yields an empty list and the caller still refuses the port."""
    import subprocess

    from tai42_e2e import ports

    def _missing(*_args, **_kwargs):
        raise FileNotFoundError("no such tool")

    monkeypatch.setattr(subprocess, "run", _missing)
    assert ports._pids_listening(1) == []
