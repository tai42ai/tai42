"""The kit's one Redis materialiser and its logged retry.

``redis_from_url_kwargs`` turns the kit's client kwargs into ``Redis.from_url`` kwargs and
``redis_connection_options`` builds the behaviour half (the retry and the timeouts) on its
own. The retry they attach is the kit's logged subclass of redis-py's ``Retry``: one
WARNING per retried failure, named by a setting, never the URL. A call made from inside
the retry's own ``do`` runs once under the outer budget; a call made from ``fail`` gets a
budget of its own. The socket cases dial loopback stubs and closed loopback ports only.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import socket
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

pytest.importorskip("redis")

import redis.asyncio.retry as async_retry_mod
import redis.connection as sync_connection_mod
import redis.retry as sync_retry_mod
from redis.backoff import ExponentialBackoff, NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tai42_kit.clients.backoff import CONNECT_RETRY_BACKOFF_BASE_SECONDS, CONNECT_RETRY_BACKOFF_CAP_SECONDS
from tai42_kit.clients.impl import redis as redis_mod

_MARKER_PASSWORD = "m4rker-pw-never-logged"
_LOGGER = redis_mod.__name__


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]


# --------------------------------------------------------------------------- #
# The materialiser
# --------------------------------------------------------------------------- #
def test_the_constants_are_redis_py_default_backoff() -> None:
    assert CONNECT_RETRY_BACKOFF_BASE_SECONDS == 0.008
    assert CONNECT_RETRY_BACKOFF_CAP_SECONDS == 0.512


@pytest.mark.parametrize(
    ("sync", "retry_cls"),
    [(False, "_LoggedAsyncRetry"), (True, "_LoggedSyncRetry")],
    ids=["async", "sync"],
)
def test_retry_attempts_attach_the_kit_logged_retry_with_the_kit_backoff(sync: bool, retry_cls: str) -> None:
    kwargs = {"url": "redis://h:6379/0", "decode_responses": True, "env_prefix": "X_", "retry_attempts": 2}
    before = dict(kwargs)
    out = redis_mod.redis_from_url_kwargs(kwargs, sync=sync)
    assert kwargs == before
    retry = out.pop("retry")
    assert type(retry) is getattr(redis_mod, retry_cls)
    assert retry._retries == 2
    assert isinstance(retry._backoff, ExponentialBackoff)
    assert retry._backoff._base == CONNECT_RETRY_BACKOFF_BASE_SECONDS
    assert retry._backoff._cap == CONNECT_RETRY_BACKOFF_CAP_SECONDS
    assert retry._label == "X_REDIS_URL"
    assert out == {
        "url": "redis://h:6379/0",
        "decode_responses": True,
        "retry_on_error": [RedisConnectionError, RedisTimeoutError, OSError],
    }


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_retry_on_timeout_alone_attaches_one_logged_retry_with_no_backoff(sync: bool) -> None:
    out = redis_mod.redis_from_url_kwargs({"url": "redis://h", "retry_on_timeout": True}, sync=sync)
    retry = out["retry"]
    assert isinstance(retry, redis_mod._LoggedSyncRetry if sync else redis_mod._LoggedAsyncRetry)
    assert retry._retries == 1
    assert isinstance(retry._backoff, NoBackoff)
    assert out["retry_on_timeout"] is True
    assert "retry_on_error" not in out


def test_no_retry_option_attaches_no_retry() -> None:
    out = redis_mod.redis_from_url_kwargs({"url": "redis://h", "socket_timeout": 2, "retry_attempts": 0})
    assert out == {"url": "redis://h", "socket_timeout": 2}


def test_the_materialiser_validates_the_url_and_the_keys() -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        redis_mod.redis_from_url_kwargs({"url": None, "env_prefix": "X_"})
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        redis_mod.redis_from_url_kwargs({"url": "redis://h", "bogus": 1})


@pytest.mark.parametrize(
    ("kwargs", "label", "expected"),
    [
        ({"url": "redis://h:1/2", "env_prefix": ""}, None, "REDIS_URL"),
        ({"url": "redis://h:1/2", "env_prefix": "ARQ_"}, None, "ARQ_REDIS_URL"),
        ({"url": "redis://h:1/2", "env_prefix": "ARQ_"}, "MY_LABEL", "MY_LABEL"),
        ({"url": f"redis://user:{_MARKER_PASSWORD}@h:1/2"}, None, "h:1/2"),
        ({"url": f"rediss://:{_MARKER_PASSWORD}@h/3"}, None, "h/3"),
        ({"url": "unix:///run/redis.sock"}, None, "/run/redis.sock"),
    ],
    ids=["base-prefix", "prefix", "explicit", "url", "url-no-port", "unix"],
)
def test_the_label_is_a_setting_name_or_the_url_without_credentials(
    kwargs: dict[str, Any], label: str | None, expected: str
) -> None:
    out = redis_mod.redis_from_url_kwargs({**kwargs, "retry_attempts": 1}, label=label)
    assert out["retry"]._label == expected


def test_the_behaviour_half_needs_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    out = redis_mod.redis_connection_options({"retry_attempts": 2, "socket_timeout": 3}, sync=True, label="L")
    retry = out.pop("retry")
    assert type(retry) is redis_mod._LoggedSyncRetry
    assert (retry._retries, retry._label) == (2, "L")
    assert out == {"socket_timeout": 3, "retry_on_error": [RedisConnectionError, RedisTimeoutError, OSError]}


@pytest.mark.parametrize("key", ["url", "max_connections", "bogus"])
def test_the_behaviour_half_refuses_any_other_key(key: str) -> None:
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        redis_mod.redis_connection_options({key: 1}, label="L")


# --------------------------------------------------------------------------- #
# The logged retry, driven directly
# --------------------------------------------------------------------------- #
def _async_retry(retries: int = 2, label: str = "L") -> Any:
    return redis_mod._LoggedAsyncRetry(ExponentialBackoff(cap=0, base=0), retries, label=label)


def _sync_retry(retries: int = 2, label: str = "L") -> Any:
    return redis_mod._LoggedSyncRetry(ExponentialBackoff(cap=0, base=0), retries, label=label)


class _Flaky:
    """A ``do`` that raises ``ConnectionError`` ``times`` times, then returns ``"ok"``."""

    def __init__(self, times: int) -> None:
        self.times = times
        self.calls = 0
        self.errors: list[BaseException] = []

    def step(self) -> str:
        self.calls += 1
        if self.calls <= self.times:
            error = RedisConnectionError(f"Error 111 connecting to h:1 #{self.calls}")
            self.errors.append(error)
            raise error
        return "ok"

    async def __call__(self) -> str:
        return self.step()


async def test_async_failures_within_the_budget_log_one_warning_each(caplog: pytest.LogCaptureFixture) -> None:
    do = _Flaky(2)
    failed: list[tuple[Any, ...]] = []

    async def fail(*args: Any) -> None:
        failed.append(args)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await _async_retry().call_with_retry(do, fail) == "ok"
    assert failed == [(do.errors[0],), (do.errors[1],)]
    assert _warnings(caplog) == [
        f"Redis connection L failed (attempt 1 of 3): {do.errors[0]}; retrying in 0.000s",
        f"Redis connection L failed (attempt 2 of 3): {do.errors[1]}; retrying in 0.000s",
    ]


def test_sync_failures_within_the_budget_log_one_warning_each(caplog: pytest.LogCaptureFixture) -> None:
    flaky = _Flaky(2)
    failed: list[tuple[Any, ...]] = []
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _sync_retry().call_with_retry(flaky.step, lambda *args: failed.append(args)) == "ok"
    assert failed == [(flaky.errors[0],), (flaky.errors[1],)]
    assert len(_warnings(caplog)) == 2


async def test_async_an_exhausted_budget_logs_n_warnings_and_raises_the_last_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    do = _Flaky(10)

    async def fail(error: BaseException) -> None:
        pass

    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError) as excinfo:
        await _async_retry().call_with_retry(do, fail)
    assert excinfo.value is do.errors[-1]
    assert do.calls == 3
    assert len(_warnings(caplog)) == 2


def test_sync_an_exhausted_budget_logs_n_warnings_and_raises_the_last_error(caplog: pytest.LogCaptureFixture) -> None:
    flaky = _Flaky(10)
    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError) as excinfo:
        _sync_retry().call_with_retry(flaky.step, lambda error: None)
    assert excinfo.value is flaky.errors[-1]
    assert flaky.calls == 3
    assert len(_warnings(caplog)) == 2


def test_the_logged_delay_is_the_next_backoff(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_retry_mod, "sleep", lambda delay: None)
    retry = redis_mod.redis_from_url_kwargs({"url": "redis://h", "retry_attempts": 2}, sync=True, label="L")["retry"]
    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(_Flaky(10).step, lambda error: None)
    assert [line.rsplit(" ", 1)[1] for line in _warnings(caplog)] == ["0.016s", "0.032s"]


def test_a_url_password_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    url = f"redis://user:{_MARKER_PASSWORD}@127.0.0.1:1/0"
    retry = redis_mod.redis_from_url_kwargs({"url": url, "retry_attempts": 1}, sync=True)["retry"]
    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(_Flaky(10).step, lambda error: None)
    assert _warnings(caplog)
    assert _MARKER_PASSWORD not in caplog.text


@pytest.mark.parametrize("make", [_async_retry, _sync_retry], ids=["async", "sync"])
def test_a_deep_copy_keeps_the_subclass_label_and_budget(make: Callable[..., Any]) -> None:
    retry = make(retries=4, label="COPY")
    copied = copy.deepcopy(retry)
    assert type(copied) is type(retry)
    assert (copied._label, copied._budget, copied._retries, copied._running) == ("COPY", 4, 4, False)


# The 7.x loop: ``call_with_retry(do, fail, is_retryable=None, with_failure_count=False)``,
# with ``fail(error, failures)`` when the count is asked for (redis 7.4.1 ``redis/retry.py``).
class _AsyncSevenLoop(async_retry_mod.Retry):
    __slots__ = ()

    async def call_with_retry(  # type: ignore[override]
        self, do: Any, fail: Any, is_retryable: Any = None, with_failure_count: bool = False
    ) -> Any:
        self._backoff.reset()
        failures = 0
        while True:
            try:
                return await do()
            except self._supported_errors as error:
                if is_retryable and not is_retryable(error):
                    raise
                failures += 1
                if with_failure_count:
                    await fail(error, failures)
                else:
                    await fail(error)
                if self._retries >= 0 and failures > self._retries:
                    raise error
                await asyncio.sleep(self._backoff.compute(failures))


class _SyncSevenLoop(sync_retry_mod.Retry):
    __slots__ = ()

    def call_with_retry(  # type: ignore[override]
        self, do: Any, fail: Any, is_retryable: Any = None, with_failure_count: bool = False
    ) -> Any:
        self._backoff.reset()
        failures = 0
        while True:
            try:
                return do()
            except self._supported_errors as error:
                if is_retryable and not is_retryable(error):
                    raise
                failures += 1
                if with_failure_count:
                    fail(error, failures)
                else:
                    fail(error)
                if self._retries >= 0 and failures > self._retries:
                    raise error


class _AsyncOverSeven(redis_mod._LoggedAsyncRetry, _AsyncSevenLoop):
    __slots__ = ()


class _SyncOverSeven(redis_mod._LoggedSyncRetry, _SyncSevenLoop):
    __slots__ = ()


async def test_async_the_seven_loop_passes_the_failure_count_through(caplog: pytest.LogCaptureFixture) -> None:
    retry = _AsyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    do = _Flaky(2)
    failed: list[tuple[Any, ...]] = []

    async def fail(error: BaseException, count: int) -> None:
        failed.append((error, count))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await retry.call_with_retry(do, fail, with_failure_count=True) == "ok"
    assert failed == [(do.errors[0], 1), (do.errors[1], 2)]
    assert len(_warnings(caplog)) == 2


def test_sync_the_seven_loop_passes_the_failure_count_through(caplog: pytest.LogCaptureFixture) -> None:
    retry = _SyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    flaky = _Flaky(2)
    failed: list[tuple[Any, ...]] = []
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert retry.call_with_retry(flaky.step, lambda e, n: failed.append((e, n)), with_failure_count=True) == "ok"
    assert failed == [(flaky.errors[0], 1), (flaky.errors[1], 2)]
    assert len(_warnings(caplog)) == 2


async def test_async_a_seven_loop_rejection_raises_at_once_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    retry = _AsyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    do = _Flaky(1)
    failed: list[object] = []

    async def fail(*args: Any) -> None:
        failed.append(args)

    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError) as excinfo:
        await retry.call_with_retry(do, fail, is_retryable=lambda e: False)
    assert excinfo.value is do.errors[0]
    assert failed == []
    assert _warnings(caplog) == []


def test_sync_a_seven_loop_rejection_raises_at_once_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    retry = _SyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    flaky = _Flaky(1)
    failed: list[object] = []
    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(flaky.step, failed.append, is_retryable=lambda e: False)
    assert failed == []
    assert _warnings(caplog) == []


async def test_async_the_override_forwards_the_callers_extra_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def spy(self: Any, do: Any, fail: Any, *args: Any, **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "ok"

    monkeypatch.setattr(async_retry_mod.Retry, "call_with_retry", spy)

    async def fail(error: BaseException) -> None:
        pass

    assert await _async_retry().call_with_retry(_Flaky(0), fail, "x", flag=True) == "ok"
    assert seen == [(("x",), {"flag": True})]


def test_sync_the_override_forwards_the_callers_extra_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def spy(self: Any, do: Any, fail: Any, *args: Any, **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "ok"

    monkeypatch.setattr(sync_retry_mod.Retry, "call_with_retry", spy)
    assert _sync_retry().call_with_retry(_Flaky(0).step, lambda error: None, "x", flag=True) == "ok"
    assert seen == [(("x",), {"flag": True})]


# --------------------------------------------------------------------------- #
# The guard: a call from inside ``do`` runs once; a call from ``fail`` has its own budget
# --------------------------------------------------------------------------- #
async def test_async_a_call_from_inside_do_runs_once_under_the_outer_budget(caplog: pytest.LogCaptureFixture) -> None:
    retry = _async_retry()
    inner_calls = 0
    inner_fails = 0

    async def inner_do() -> None:
        nonlocal inner_calls
        inner_calls += 1
        raise ConnectionRefusedError("refused")

    async def inner_fail(error: BaseException) -> None:
        nonlocal inner_fails
        inner_fails += 1

    async def outer_do() -> None:
        try:
            await retry.call_with_retry(inner_do, inner_fail)
        except OSError as exc:
            raise RedisConnectionError(str(exc)) from exc

    async def outer_fail(error: BaseException) -> None:
        pass

    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
        await retry.call_with_retry(outer_do, outer_fail)
    assert inner_calls == 3
    assert inner_fails == 0
    assert len(_warnings(caplog)) == 2


def test_sync_a_call_from_inside_do_runs_once_under_the_outer_budget(caplog: pytest.LogCaptureFixture) -> None:
    retry = _sync_retry()
    calls = {"inner": 0, "inner_fail": 0}

    def inner_do() -> None:
        calls["inner"] += 1
        raise ConnectionRefusedError("refused")

    def inner_fail(error: BaseException) -> None:
        calls["inner_fail"] += 1

    def outer_do() -> None:
        try:
            retry.call_with_retry(inner_do, inner_fail)
        except OSError as exc:
            raise RedisConnectionError(str(exc)) from exc

    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(outer_do, lambda error: None)
    assert calls == {"inner": 3, "inner_fail": 0}
    assert len(_warnings(caplog)) == 2


async def test_async_a_call_from_fail_has_a_budget_of_its_own(caplog: pytest.LogCaptureFixture) -> None:
    retry = _async_retry()
    retry.update_supported_errors([OSError])
    inner_calls = 0

    async def inner_do() -> None:
        nonlocal inner_calls
        inner_calls += 1
        raise ConnectionRefusedError("refused")

    async def inner_fail(error: BaseException) -> None:
        pass

    async def outer_do() -> None:
        raise RedisConnectionError("read lost")

    async def outer_fail(error: BaseException) -> None:
        await retry.call_with_retry(inner_do, inner_fail)

    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(ConnectionRefusedError):
        await retry.call_with_retry(outer_do, outer_fail)
    assert inner_calls == 3
    assert len(_warnings(caplog)) == 2


def test_sync_a_call_from_fail_has_a_budget_of_its_own(caplog: pytest.LogCaptureFixture) -> None:
    retry = _sync_retry()
    retry.update_supported_errors([OSError])
    calls = 0

    def inner_do() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError("refused")

    def outer_do() -> None:
        raise RedisConnectionError("read lost")

    def outer_fail(error: BaseException) -> None:
        retry.call_with_retry(inner_do, lambda error: None)

    with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(ConnectionRefusedError):
        retry.call_with_retry(outer_do, outer_fail)
    assert calls == 3
    assert len(_warnings(caplog)) == 2


async def test_async_the_guard_is_cleared_after_a_raise_and_after_a_cancel_in_the_backoff() -> None:
    retry = _async_retry()

    async def fail(error: BaseException) -> None:
        pass

    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(_Flaky(10), fail)
    assert retry._running is False
    again = _Flaky(10)
    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(again, fail)
    assert again.calls == 3

    # A long backoff, so the call is parked in its sleep when it is cancelled.
    retry._backoff = ExponentialBackoff(cap=60, base=60)
    sleeping = _Flaky(10)
    task = asyncio.create_task(retry.call_with_retry(sleeping, fail))
    while sleeping.calls == 0:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleeping.calls == 1
    assert retry._running is False

    retry._backoff = ExponentialBackoff(cap=0, base=0)
    fresh = _Flaky(10)
    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(fresh, fail)
    assert fresh.calls == 3


def test_sync_the_guard_is_cleared_after_a_raise() -> None:
    retry = _sync_retry()
    with pytest.raises(RedisConnectionError):
        retry.call_with_retry(_Flaky(10).step, lambda error: None)
    assert retry._running is False
    again = _Flaky(10)
    with pytest.raises(RedisConnectionError):
        retry.call_with_retry(again.step, lambda error: None)
    assert again.calls == 3


# --------------------------------------------------------------------------- #
# Real sockets: the pool's connect, a command's reconnect, a pub/sub read's reconnect
# --------------------------------------------------------------------------- #
def _closed_loopback_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _ConnectCounter:
    def __init__(self) -> None:
        self.calls = 0


def _count_async_connects(monkeypatch: pytest.MonkeyPatch) -> _ConnectCounter:
    counter = _ConnectCounter()
    original = redis_mod._TcpConnection._connect

    async def _connect(self: Any) -> None:
        counter.calls += 1
        await original(self)

    monkeypatch.setattr(redis_mod._TcpConnection, "_connect", _connect)
    return counter


def _count_sync_connects(monkeypatch: pytest.MonkeyPatch) -> _ConnectCounter:
    counter = _ConnectCounter()
    original = sync_connection_mod.Connection._connect

    def _connect(self: Any) -> Any:
        counter.calls += 1
        return original(self)

    monkeypatch.setattr(sync_connection_mod.Connection, "_connect", _connect)
    return counter


async def test_async_the_pool_connect_retries_a_refused_port_with_warnings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = _count_async_connects(monkeypatch)
    url = f"redis://127.0.0.1:{_closed_loopback_port()}/0"
    client = await redis_mod.RedisClient()._create(url=url, retry_attempts=2, env_prefix="X_")
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
            await client.ping()
    finally:
        await client.aclose()
    assert counter.calls == 3
    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert warnings[0].startswith("Redis connection X_REDIS_URL failed (attempt 1 of 3): ")


async def test_sync_the_pool_connect_retries_a_refused_port_with_warnings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = _count_sync_connects(monkeypatch)
    url = f"redis://127.0.0.1:{_closed_loopback_port()}/0"
    client = await redis_mod.SyncRedisClient()._create(url=url, retry_attempts=2)
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
            client.ping()
    finally:
        client.close()
    assert counter.calls == 3
    assert len(_warnings(caplog)) == 2


class _Stub:
    """A loopback Redis stub: answers PING, SUBSCRIBE and anything else; can drop and come back."""

    def __init__(self, *, drop_on_ping: int | None = None) -> None:
        self.port = 0
        self.servers: list[asyncio.Server] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.subscribes = 0
        self.pings = 0
        self.drop_on_ping = drop_on_ping

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        try:
            while header := await reader.readline():
                words = []
                for _ in range(int(header[1:])):
                    length = int((await reader.readline())[1:])
                    words.append((await reader.readexactly(length + 2))[:-2])
                command = words[0].upper()
                if command == b"PING":
                    self.pings += 1
                    if self.pings == self.drop_on_ping:
                        self.drop()
                        return
                    writer.write(b"+PONG\r\n")
                elif command == b"SUBSCRIBE":
                    self.subscribes += 1
                    for count, channel in enumerate(words[1:], 1):
                        writer.write(b"*3\r\n$9\r\nsubscribe\r\n$%d\r\n%s\r\n:%d\r\n" % (len(channel), channel, count))
                else:
                    writer.write(b"+OK\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)
        self.port = server.sockets[0].getsockname()[1]
        self.servers.append(server)

    def drop(self) -> None:
        """Stop listening and abort every open connection."""
        for server in self.servers:
            server.close()
        for writer in self.writers:
            writer.transport.abort()
        self.writers.clear()

    async def drop_closed(self) -> None:
        """Drop, then wait until every aborted connection's socket is closed.

        ``abort`` closes a socket on the loop's next pass; a client that sends
        before then reaches the old socket, and its send succeeds.
        """
        writers = list(self.writers)
        self.drop()
        for writer in writers:
            await writer.wait_closed()

    def come_back(self, loop: asyncio.AbstractEventLoop) -> None:
        """Listen again on the same port at once; the stub starts answering on the loop."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.listen()

        async def serve() -> None:
            self.servers.append(await asyncio.start_server(self.handle, sock=sock))

        loop.call_soon_threadsafe(lambda: loop.create_task(serve()))

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"


@asynccontextmanager
async def _stub(**kwargs: Any) -> AsyncIterator[_Stub]:
    stub = _Stub(**kwargs)
    await stub.start()
    try:
        yield stub
    finally:
        stub.drop()


class _OnWarning(logging.Handler):
    """Runs ``action`` once, when the ``nth`` kit WARNING is emitted."""

    def __init__(self, nth: int, action: Callable[[], None]) -> None:
        super().__init__(logging.WARNING)
        self.nth, self.action, self.seen = nth, action, 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seen += 1
        if self.seen == self.nth:
            self.action()


@asynccontextmanager
async def _on_warning(nth: int, action: Callable[[], None]) -> AsyncIterator[None]:
    handler = _OnWarning(nth, action)
    logger = logging.getLogger(_LOGGER)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


async def test_async_a_connection_lost_mid_command_reconnects_under_the_command_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with _stub(drop_on_ping=2) as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            assert await client.ping() is True
            counter = _count_async_connects(monkeypatch)
            with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
                await client.ping()
        finally:
            await client.aclose()
    assert counter.calls == 2
    assert len(_warnings(caplog)) == 2


async def test_sync_a_connection_lost_mid_command_reconnects_under_the_command_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with _stub(drop_on_ping=2) as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            assert await asyncio.to_thread(client.ping) is True
            counter = _count_sync_connects(monkeypatch)
            with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
                await asyncio.to_thread(client.ping)
        finally:
            client.close()
    assert counter.calls == 2
    assert len(_warnings(caplog)) == 2


async def test_async_a_pubsub_read_rides_a_comeback_within_its_reconnect_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with _stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("events")
            subscribed = await pubsub.get_message(timeout=1)
            assert subscribed is not None
            assert subscribed["type"] == "subscribe"
            counter = _count_async_connects(monkeypatch)
            stub.drop()
            loop = asyncio.get_running_loop()
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                async with _on_warning(2, lambda: stub.come_back(loop)):
                    message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert message is not None
    assert message["type"] == "subscribe"
    assert counter.calls == 3
    assert len(_warnings(caplog)) == 3
    assert stub.subscribes == 2


async def test_async_a_pubsub_read_refused_past_its_budget_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with _stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            counter = _count_async_connects(monkeypatch)
            stub.drop()
            with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
                await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert counter.calls == 3
    assert len(_warnings(caplog)) == 2


async def test_sync_a_pubsub_read_rides_a_comeback_within_its_reconnect_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with _stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            subscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert subscribed is not None
            assert subscribed["type"] == "subscribe"
            counter = _count_sync_connects(monkeypatch)
            stub.drop()
            loop = asyncio.get_running_loop()
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                async with _on_warning(2, lambda: stub.come_back(loop)):
                    message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert message is not None
    assert message["type"] == "subscribe"
    assert counter.calls == 3
    assert len(_warnings(caplog)) == 3
    assert stub.subscribes == 2


async def test_sync_a_pubsub_read_refused_past_its_budget_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with _stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            counter = _count_sync_connects(monkeypatch)
            stub.drop()
            with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(RedisConnectionError):
                await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert counter.calls == 3
    assert len(_warnings(caplog)) == 2


async def test_sync_a_pubsub_command_whose_send_fails_reconnects_and_resubscribes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with _stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        done = threading.Event()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            subscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert subscribed is not None
            assert subscribed["type"] == "subscribe"
            await stub.drop_closed()
            stub.come_back(asyncio.get_running_loop())

            def subscribe_more() -> None:
                # The second send at the latest meets the dropped socket; its retry's
                # reconnect re-subscribes "events" while the failed command is in flight.
                pubsub.subscribe("alerts")
                pubsub.subscribe("status")
                done.set()

            # A daemon thread, so a command that never returns fails the test instead of hanging it.
            worker = threading.Thread(target=subscribe_more, daemon=True)
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                worker.start()
                await asyncio.to_thread(worker.join, 5)
            assert done.is_set(), "the pub/sub command never returned after its reconnect"
            # A send returns before the stub reads it; the stub answers each SUBSCRIBE on
            # one connection in order, so its answer to "status" means it counted them all.
            replies: list[tuple[Any, Any]] = []
            while len(replies) < 3:
                reply = await asyncio.to_thread(pubsub.get_message, timeout=5)
                assert reply is not None, f"the stub stopped answering after {replies}"
                replies.append((reply["type"], reply["channel"]))
        finally:
            if done.is_set():
                pubsub.close()
            client.close()
    assert len(_warnings(caplog)) == 1
    # Every channel is subscribed on the new connection, the re-sent "status" last.
    assert replies == [("subscribe", b"events"), ("subscribe", b"alerts"), ("subscribe", b"status")]
    assert stub.subscribes >= 3


async def test_async_a_pubsub_runs_its_reconnect_hook_after_each_re_subscribe() -> None:
    async with _stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            assert reconnects == []
            stub.drop()
            loop = asyncio.get_running_loop()
            async with _on_warning(2, lambda: stub.come_back(loop)):
                message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert message is not None
    assert message["type"] == "subscribe"
    # Once, on the reconnect, which subscribed "events" again.
    assert reconnects == [None]
    assert stub.subscribes == 2


async def test_sync_a_pubsub_runs_its_reconnect_hook_after_each_re_subscribe() -> None:
    async with _stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert reconnects == []
            stub.drop()
            loop = asyncio.get_running_loop()
            async with _on_warning(2, lambda: stub.come_back(loop)):
                message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert message is not None
    assert message["type"] == "subscribe"
    assert reconnects == [None]
    assert stub.subscribes == 2
