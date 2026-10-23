"""The Postgres seam retries establishing its first connection, never a statement.

``_open_named_pool`` proves the first connection before it builds a pool; with
``retry_attempts`` set, a ``psycopg.OperationalError`` from that probe is retried with the
kit's exponential backoff, one WARNING per retried failure, and the last error is raised
unchanged with a note naming the attempts. Every door that opens a pool reaches it: the
pooled client (also ``fresh=True`` and the rebuild after an eviction), a dedicated pool and
a pinned connection. Hermetic: the probe connect and ``asyncio.sleep`` are fakes, except
the refused-port case, which dials a closed loopback port.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

import psycopg
from pydantic import SecretStr
from tai42_contract.errors import ClientDisconnectedError

from tai42_kit.clients import PostgresConnectionOptions, PostgresConnectionSettings, client_ctx, shutdown_all_clients
from tai42_kit.clients.impl import postgres as pg_mod

_MARKER_PASSWORD = "m4rker-pw-never-logged"
_DSN = f"postgresql://u:{_MARKER_PASSWORD}@h:5433/db"
_POOL_NAME = "postgres@h/db"


class _FakeProbe:
    async def __aenter__(self) -> _FakeProbe:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Connects:
    """A fake ``AsyncConnection.connect``: raises the queued errors in order, then succeeds."""

    def __init__(self, errors: Sequence[BaseException]) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self, conninfo: str, **kwargs: Any) -> _FakeProbe:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _FakeProbe()


class _Pools:
    """A fake pool class that records every pool built."""

    def __init__(self) -> None:
        self.built: list[Any] = []
        record = self.built

        class _FakePool:
            @staticmethod
            async def check_connection(conn: object) -> None:
                pass

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                record.append(self)

            async def open(self, *, wait: bool = False, timeout: float | None = None) -> None:
                pass

            async def close(self) -> None:
                pass

            def connection(self) -> Any:
                return _FakeProbe()

        self.cls = _FakePool


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(pg_mod.asyncio, "sleep", _sleep)
    return recorded


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> _Pools:
    fake = _Pools()
    monkeypatch.setattr(pg_mod, "_CancelSurfacingPool", fake.cls)
    return fake


def _install_connects(monkeypatch: pytest.MonkeyPatch, errors: Sequence[BaseException]) -> _Connects:
    connects = _Connects(errors)
    monkeypatch.setattr(pg_mod.AsyncConnection, "connect", connects)
    return connects


def _refused(i: int = 0) -> psycopg.OperationalError:
    return psycopg.OperationalError(f"connection to server at 'h', port 5433 failed: refused #{i}")


async def _open(retry_attempts: int) -> Any:
    return await pg_mod._open_named_pool(_DSN, min_size=1, max_size=1, name=_POOL_NAME, retry_attempts=retry_attempts)


async def test_failures_within_the_budget_are_ridden_out_and_the_pool_is_built(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    connects = _install_connects(monkeypatch, [_refused(1), _refused(2)])
    pool = await _open(3)
    assert connects.calls == 3
    assert pools.built == [pool]


async def test_the_backoff_doubles_from_16ms_and_is_capped_at_512ms(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    _install_connects(monkeypatch, [_refused(i) for i in range(7)])
    await _open(7)
    # redis-py's ExponentialBackoff(): base 8 ms doubled per failure, capped at 512 ms.
    assert sleeps == [0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 0.512]


async def test_an_exhausted_budget_raises_the_last_driver_error_with_a_note_and_builds_no_pool(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    errors = [_refused(i) for i in range(3)]
    last = errors[-1]
    connects = _install_connects(monkeypatch, errors)
    with pytest.raises(psycopg.OperationalError) as excinfo:
        await _open(2)
    assert excinfo.value is last
    assert excinfo.value.__notes__ == [f"Postgres connection {_POOL_NAME!r} failed after 3 attempt(s)"]
    assert connects.calls == 3
    assert pools.built == []


async def test_zero_attempts_is_one_connect_with_the_plain_error_and_no_sleep(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    error = _refused()
    connects = _install_connects(monkeypatch, [error])
    with pytest.raises(psycopg.OperationalError) as excinfo:
        await _open(0)
    assert excinfo.value is error
    assert str(excinfo.value) == str(_refused())
    assert connects.calls == 1
    assert sleeps == []
    assert pools.built == []


async def test_each_retried_failure_logs_one_warning_naming_the_pool_never_the_password(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools, caplog: pytest.LogCaptureFixture
) -> None:
    _install_connects(monkeypatch, [_refused(1), _refused(2)])
    with caplog.at_level(logging.WARNING, logger=pg_mod.__name__):
        await _open(3)
    warnings = [r for r in caplog.records if r.name == pg_mod.__name__ and r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        f"Postgres connection {_POOL_NAME!r} failed (attempt 1 of 4): {_refused(1)}; retrying in 0.016s",
        f"Postgres connection {_POOL_NAME!r} failed (attempt 2 of 4): {_refused(2)}; retrying in 0.032s",
    ]
    assert _MARKER_PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "error", [psycopg.ProgrammingError("bad dsn"), RuntimeError("boom")], ids=["programming", "runtime"]
)
async def test_a_non_operational_error_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools, error: Exception
) -> None:
    connects = _install_connects(monkeypatch, [error])
    with pytest.raises(type(error)) as excinfo:
        await _open(3)
    assert excinfo.value is error
    assert connects.calls == 1
    assert sleeps == []


async def test_a_cancel_during_the_backoff_sleep_propagates_with_no_further_connect(
    monkeypatch: pytest.MonkeyPatch, pools: _Pools
) -> None:
    connects = _install_connects(monkeypatch, [_refused(1), _refused(2)])

    async def _cancelled_sleep(delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(pg_mod.asyncio, "sleep", _cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await _open(3)
    assert connects.calls == 1
    assert pools.built == []


@pytest.mark.parametrize("value", [-1, True, "2"], ids=["negative", "bool", "str"])
async def test_the_client_rejects_a_retry_attempts_that_is_not_a_non_negative_int(value: object) -> None:
    with pytest.raises(ValueError, match="'retry_attempts' must be a non-negative int"):
        await pg_mod.PostgresClient()._create(dsn=_DSN, retry_attempts=value)


# --------------------------------------------------------------------------- #
# Every door that opens a pool reaches the retry
# --------------------------------------------------------------------------- #
def _settings(retry_attempts: int) -> PostgresConnectionSettings:
    return PostgresConnectionSettings(
        pg_host="h",
        pg_port=5433,
        pg_db="db",
        pg_user="u",
        pg_password=SecretStr(_MARKER_PASSWORD),
        pg_retry_attempts=retry_attempts,
    )


async def test_a_dedicated_pool_on_settings_retries(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    connects = _install_connects(monkeypatch, [_refused(1), _refused(2)])
    await pg_mod.open_dedicated_pool(_settings(2), role="checkpoint")
    assert connects.calls == 3


async def test_a_dedicated_pool_on_a_dsn_retries_by_its_options(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    connects = _install_connects(monkeypatch, [_refused(1), _refused(2)])
    await pg_mod.open_dedicated_pool(_DSN, role="store", options=PostgresConnectionOptions(pg_retry_attempts=2))
    assert connects.calls == 3


async def test_a_dedicated_pool_on_a_dsn_without_options_tries_once_and_reads_no_base_env(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    monkeypatch.setenv("PG_RETRY_ATTEMPTS", "2")
    connects = _install_connects(monkeypatch, [_refused(1)])
    with pytest.raises(psycopg.OperationalError):
        await pg_mod.open_dedicated_pool(_DSN, role="store")
    assert connects.calls == 1


async def test_a_dedicated_pool_on_settings_refuses_separate_options() -> None:
    with pytest.raises(TypeError, match="options"):
        await pg_mod.open_dedicated_pool(_settings(0), role="store", options=PostgresConnectionOptions())


async def test_a_pinned_connection_retries(monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools) -> None:
    connects = _install_connects(monkeypatch, [_refused(1), _refused(2)])
    async with pg_mod.pinned_connection(_settings(2)):
        pass
    assert connects.calls == 3


async def test_a_fresh_client_retries(monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools) -> None:
    connects = _install_connects(monkeypatch, [_refused(1), _refused(2)])
    async with client_ctx(pg_mod.PostgresClient, _settings(2), fresh=True):
        pass
    assert connects.calls == 3


async def test_the_rebuild_after_an_eviction_retries(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], pools: _Pools
) -> None:
    class _PG(pg_mod.PostgresClient):
        pass

    connects = _install_connects(monkeypatch, [])
    kwargs = _settings(2).client_kwargs()
    try:
        with pytest.raises(ClientDisconnectedError):
            async with _PG().current(**kwargs):
                raise _refused()
        connects.errors[:] = [_refused(1), _refused(2)]
        async with _PG().current(**kwargs):
            pass
        # One connect for the first build, then three for the rebuild after the eviction.
        assert connects.calls == 4
        assert len(pools.built) == 2
    finally:
        await shutdown_all_clients()


def _closed_loopback_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def test_a_real_refused_port_is_dialled_once_per_attempt_then_raises_with_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = pg_mod.AsyncConnection.connect
    calls = 0

    async def _counting_connect(conninfo: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await real_connect(conninfo, **kwargs)

    monkeypatch.setattr(pg_mod.AsyncConnection, "connect", _counting_connect)
    dsn = f"postgresql://u:{_MARKER_PASSWORD}@127.0.0.1:{_closed_loopback_port()}/db?connect_timeout=2"
    with pytest.raises(psycopg.OperationalError) as excinfo:
        await pg_mod._open_named_pool(dsn, min_size=1, max_size=1, name="refused", retry_attempts=2)
    assert calls == 3
    assert excinfo.value.__notes__ == ["Postgres connection 'refused' failed after 3 attempt(s)"]
