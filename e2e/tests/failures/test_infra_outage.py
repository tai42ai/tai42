"""A Redis or Postgres outage injected per-stack through a TCP relay.

The shared compose container cannot be stopped to model an outage (it would take
down every other concurrently-alive stack), so each store is fronted by a
per-stack :class:`~tai42_e2e.tcprelay.TcpRelay`: severing it drops the SUT's live
connections and refuses new ones, restoring it re-opens the same port. Severing
mid-run must make a request touching that store fail LOUDLY at the HTTP surface
(5xx, never a hang past the request timeout, never a silent success); restoring it
must recover the stack WITHOUT a restart (the connection pools re-establish).

With a connection retry configured, the outage is logged once per retried failure
(the kit's ``Redis connection … failed (attempt`` / ``Postgres connection … failed
(attempt`` WARNINGs): an outage shorter than the retry window is ridden out, and a
longer one still fails loudly once the attempts run out. The backend legs show the
same on the worker, the rq beat and the celery result backend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pytest
import redis

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.manifests.core import build_core_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready
from tai42_e2e.topology import StackConfig, StackResources

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

pytestmark = pytest.mark.needs("process")

RelayedStack = tuple[TaiStack, TcpRelay, TcpRelay]

# A run on the worker returns within this, or the leg fails rather than hangs.
_WORKER_RUN_DEADLINE = 90.0
_POSTGRES_RETRY_WARNING = ("Postgres connection ", "failed (attempt")
# The scheduler key's expiry after a pass of the variant's ``beat -i 1``.
_BEAT_KEY_TTL = 11
_BACKEND_NEEDS = ("kind:backend", "probe-tools", "setting:extension:sync_task", "setting:harness-backend-variant")


@pytest.fixture
def relayed_core_stack(infra: Infra, fresh_stack: Callable[..., TaiStack]) -> Callable[..., RelayedStack]:
    """A factory for a stack whose feature Redis and Postgres are reached through
    per-stack relays. The harness still allocates + seeds the real logical Redis DB and
    PG clone; only the SUT's connection ENDPOINTS are rerouted, through
    ``allocate_resources``' endpoint overrides, so a test can sever them mid-run.
    The stack adopts both relays, so teardown stops and leak-checks them.
    ``env_overrides`` reach every process of the stack; ``builder`` shapes it."""

    def make(
        *,
        env_overrides: dict[str, str] | None = None,
        builder: Callable[[StackResources, Variants], StackConfig] = build_core_stack,
    ) -> RelayedStack:
        redis_host, redis_port = infra.settings.redis_host_port
        redis_relay = TcpRelay(redis_host, redis_port)
        pg_relay = TcpRelay(infra.settings.pg_host, infra.settings.pg_port)
        try:
            redis_relay.start()
            pg_relay.start()
            wait_relay_ready(redis_relay)
            wait_relay_ready(pg_relay)
        except BaseException:
            redis_relay.stop()
            pg_relay.stop()
            raise

        stack = fresh_stack(
            builder,
            env_overrides=env_overrides,
            resource_kwargs={
                "redis_host": redis_relay.listen_host,
                "redis_port": redis_relay.port,
                "pg_host": pg_relay.listen_host,
                "pg_port": pg_relay.port,
            },
            relays=[redis_relay, pg_relay],
        )
        return stack, redis_relay, pg_relay

    return make


def _one_serve_worker(res: StackResources, variants: Variants) -> StackConfig:
    """The core stack served by one worker, so every request reaches the same pools."""
    return replace(build_core_stack(res, variants), workers=1)


def _log_after(stack: TaiStack, name: str, offset: int) -> str:
    """What process ``name`` logged after ``offset``."""
    return stack.process(name).log_path.read_text(encoding="utf-8", errors="replace")[offset:]


def _log_size(stack: TaiStack, name: str) -> int:
    return len(_log_after(stack, name, 0))


def _lines_with(text: str, *markers: str) -> list[str]:
    return [line for line in text.splitlines() if all(marker in line for marker in markers)]


async def _wait_for_log(stack: TaiStack, name: str, offset: int, *markers: str, deadline: float = 30.0) -> None:
    async def logged() -> bool:
        return bool(_lines_with(_log_after(stack, name, offset), *markers))

    await wait_for_async(logged, deadline=deadline, message=f"{name} never logged {markers!r}")


def _skip_unless_backend(infra: Infra, *names: str, reason: str) -> None:
    backend = infra.variants.backend.name
    if backend not in names:
        pytest.skip(f"{backend}: {reason}")


async def _run_on_worker(stack: TaiStack, payload: str) -> Any:
    """Run ``e2e_echo(payload)`` on the backend worker and return the call result, bounded."""
    async with stack.mcp() as mcp:
        return await asyncio.wait_for(
            mcp.call_tool(
                "run_tool_sync_task",
                {"tool_name": "e2e_echo", "arguments": {"payload": payload}},
                raise_on_error=False,
                retry_on_reloading=True,
            ),
            timeout=_WORKER_RUN_DEADLINE,
        )


def _assert_two_retry_warnings(warnings: list[str]) -> None:
    """Two retries log exactly two WARNINGs, the first and second of three attempts."""
    assert len(warnings) == 2, "\n".join(warnings)
    assert "attempt 1 of 3" in warnings[0], warnings
    assert "attempt 2 of 3" in warnings[1], warnings


def _echoed(result: Any) -> Any:
    return result.data if result.data is not None else result.structured_content


@pytest.mark.needs("probe-tools", "store:redis")
async def test_redis_outage_fails_loudly_then_recovers(
    relayed_core_stack: Callable[..., RelayedStack],
) -> None:
    stack, redis_relay, _pg_relay = relayed_core_stack()
    api = stack.api()
    # Submitting a background run writes to the tool-runs Redis store; healthy,
    # it returns 202.
    await api.post("/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}, expect=202)

    redis_relay.sever()
    # The same request now fails LOUDLY at the HTTP surface — a 5xx, within the
    # client timeout (a hang would raise TimeoutException, failing the test and
    # surfacing a silent-hang product bug).
    resp = await api.request_raw(
        "POST", "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}
    )
    assert resp.status_code >= 500, f"expected a loud 5xx during the redis outage, got {resp.status_code}"

    redis_relay.restore()
    wait_relay_ready(redis_relay)

    # Recovery WITHOUT a restart: the pooled clients re-establish and a fresh
    # request succeeds again.
    async def submits() -> bool:
        r = await api.request_raw(
            "POST", "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}
        )
        return r.status_code == 202

    await wait_for_async(submits, deadline=20.0, message="stack never recovered after redis was restored")


@pytest.mark.needs("probe-tools", "store:redis")
async def test_redis_retry_is_logged_then_fails_loudly(
    relayed_core_stack: Callable[..., RelayedStack],
) -> None:
    stack, redis_relay, _pg_relay = relayed_core_stack(env_overrides={"TAI_TOOL_RUNS_RETRY_ATTEMPTS": "2"})
    api = stack.api()
    body = {"tool_name": "e2e_echo", "arguments": {"payload": "x"}}
    run_id = (await api.post("/api/tool-runs", json=body, expect=202))["run_id"]

    # The run writes its record as it goes; once it has succeeded, the request below is
    # the only use of the tool-runs Redis during the outage.
    async def succeeded() -> bool:
        return (await api.get(f"/api/tool-runs/{run_id}"))["status"] == "succeeded"

    await wait_for_async(succeeded, deadline=20.0, message=f"run {run_id} never succeeded")

    redis_relay.sever()
    offset = _log_size(stack, "serve")
    resp = await api.request_raw("POST", "/api/tool-runs", json=body)
    assert resp.status_code >= 500, f"expected a loud 5xx once the retries ran out, got {resp.status_code}"

    # Two retries: one WARNING per retried failure, then the error.
    warnings = _lines_with(
        _log_after(stack, "serve", offset), "Redis connection TAI_TOOL_RUNS_REDIS_URL failed (attempt"
    )
    _assert_two_retry_warnings(warnings)

    redis_relay.restore()
    wait_relay_ready(redis_relay)

    async def submits() -> bool:
        return (await api.request_raw("POST", "/api/tool-runs", json=body)).status_code == 202

    await wait_for_async(submits, deadline=20.0, message="stack never recovered after redis was restored")


@pytest.mark.needs("store:postgres")
async def test_postgres_outage_fails_loudly_then_recovers(
    relayed_core_stack: Callable[..., RelayedStack],
) -> None:
    stack, _redis_relay, pg_relay = relayed_core_stack()
    api = stack.api()
    # Listing presets reads the versioned Postgres store; healthy, it returns
    # 200 with a (possibly empty) list.
    await api.get("/api/presets", expect=200)

    pg_relay.sever()
    resp = await api.request_raw("GET", "/api/presets")
    assert resp.status_code >= 500, f"expected a loud 5xx during the postgres outage, got {resp.status_code}"

    pg_relay.restore()
    wait_relay_ready(pg_relay)

    async def lists() -> bool:
        r = await api.request_raw("GET", "/api/presets")
        return r.status_code == 200

    await wait_for_async(lists, deadline=20.0, message="stack never recovered after postgres was restored")


async def _sever_and_evict_the_postgres_pool(stack: TaiStack, pg_relay: TcpRelay) -> None:
    """Sever Postgres under a healthy pool: the first request on the dead connections
    fails and evicts the pool, so the next request builds a new one."""
    api = stack.api()
    await api.get("/api/presets", expect=200)
    pg_relay.sever()
    offset = _log_size(stack, "serve")
    resp = await api.request_raw("GET", "/api/presets")
    assert resp.status_code >= 500, f"expected a loud 5xx on the severed pool, got {resp.status_code}"
    assert _lines_with(_log_after(stack, "serve", offset), "disconnected and was removed from the cache"), (
        "the failed request did not evict the postgres pool"
    )


@pytest.mark.needs("store:postgres", "topology:workers=1")
async def test_postgres_outage_rides_out_on_connect_retry(
    relayed_core_stack: Callable[..., RelayedStack],
) -> None:
    # Thirty retries are a backoff window far longer than the outage below.
    stack, _redis_relay, pg_relay = relayed_core_stack(
        env_overrides={"TAI_DATABASE_DEFAULT_PG_RETRY_ATTEMPTS": "30"}, builder=_one_serve_worker
    )
    await _sever_and_evict_the_postgres_pool(stack, pg_relay)

    offset = _log_size(stack, "serve")
    request = asyncio.create_task(stack.api().request_raw("GET", "/api/presets"))
    try:
        await _wait_for_log(stack, "serve", offset, *_POSTGRES_RETRY_WARNING)
    except BaseException:
        request.cancel()
        raise
    pg_relay.restore()
    wait_relay_ready(pg_relay)

    # The request that met the outage rode it out on the connect retry.
    resp = await asyncio.wait_for(request, timeout=60.0)
    assert resp.status_code == 200, f"expected the retried request to succeed, got {resp.status_code}"


@pytest.mark.needs("store:postgres", "topology:workers=1")
async def test_postgres_connect_retry_exhausted_fails_loudly(
    relayed_core_stack: Callable[..., RelayedStack],
) -> None:
    stack, _redis_relay, pg_relay = relayed_core_stack(
        env_overrides={"TAI_DATABASE_DEFAULT_PG_RETRY_ATTEMPTS": "2"}, builder=_one_serve_worker
    )
    await _sever_and_evict_the_postgres_pool(stack, pg_relay)

    offset = _log_size(stack, "serve")
    resp = await stack.api().request_raw("GET", "/api/presets")
    assert resp.status_code >= 500, f"expected a loud 5xx once the retries ran out, got {resp.status_code}"

    logged = _log_after(stack, "serve", offset)
    warnings = _lines_with(logged, *_POSTGRES_RETRY_WARNING)
    _assert_two_retry_warnings(warnings)
    note = _lines_with(logged, "Postgres connection ", "failed after 3 attempt(s)")
    assert note, f"the final error carries no attempt note:\n{logged}"
    assert logged.index(warnings[-1]) < logged.index(note[0]), logged


@pytest.mark.needs(*_BACKEND_NEEDS, "store:redis")
async def test_celery_result_backend_retry_is_logged_then_fails_loudly(
    infra: Infra, relayed_core_stack: Callable[..., RelayedStack]
) -> None:
    _skip_unless_backend(infra, "celery", reason="only the celery variant keeps task results on a Redis result backend")
    stack, redis_relay, _pg_relay = relayed_core_stack(env_overrides={"CELERY_RESULT_BACKEND_RETRY_ATTEMPTS": "2"})
    assert _echoed(await _run_on_worker(stack, "before")) == "before"

    redis_relay.sever()
    offsets = {name: _log_size(stack, name) for name in ("serve", "backend")}
    # The server subscribes to the run's result before it publishes the task over
    # RabbitMQ, and the worker writes the result. Whichever of them meets the severed
    # Redis first fails the run: the server's subscribe fails before the task is sent,
    # or the task reaches the worker and its writes fail. Each Redis call either makes
    # is retried twice and then fails: two WARNINGs per call, never a third attempt.
    failed = await _run_on_worker(stack, "during")
    assert failed.is_error, f"expected the run to fail while the result backend is down, got {failed!r}"
    warnings = [
        line
        for name, offset in offsets.items()
        for line in _lines_with(
            _log_after(stack, name, offset), "Redis connection CELERY_RESULT_BACKEND failed (attempt"
        )
    ]
    first = [line for line in warnings if "attempt 1 of 3" in line]
    second = [line for line in warnings if "attempt 2 of 3" in line]
    assert first, "the result backend logged no retry"
    assert len(first) == len(second), "\n".join(warnings)
    assert len(first) + len(second) == len(warnings), "\n".join(warnings)

    redis_relay.restore()
    wait_relay_ready(redis_relay)

    async def runs() -> bool:
        return _echoed(await _run_on_worker(stack, "after")) == "after"

    await wait_for_async(runs, deadline=60.0, message="no run succeeded after redis was restored")


@pytest.mark.needs(*_BACKEND_NEEDS, "store:redis")
async def test_worker_rides_a_redis_restart_while_waiting(
    infra: Infra, relayed_core_stack: Callable[..., RelayedStack]
) -> None:
    _skip_unless_backend(
        infra, "arq", "rq", reason="the worker waits on RabbitMQ here; the plugin's live test covers a Redis broker"
    )
    if infra.variants.backend.name == "arq":
        # arq's poll loop has no reconnect: the seam's retry, a window longer than the
        # outage, is what rides it.
        stack, redis_relay, _pg_relay = relayed_core_stack(env_overrides={"ARQ_RETRY_ATTEMPTS": "30"})
        markers: tuple[str, ...] = ("Redis connection ARQ_REDIS_URL failed (attempt",)
    else:
        # rq's dequeue loop reconnects on its own, logging each failed reconnect at ERROR.
        stack, redis_relay, _pg_relay = relayed_core_stack()
        markers = ("could not connect to Redis instance",)
    assert _echoed(await _run_on_worker(stack, "before")) == "before"

    offset = _log_size(stack, "backend")
    redis_relay.sever()
    await _wait_for_log(stack, "backend", offset, *markers)
    redis_relay.restore()
    wait_relay_ready(redis_relay)

    assert stack.process("backend").is_running(), stack.process("backend").log_tail()
    assert _echoed(await _run_on_worker(stack, "after")) == "after"


@pytest.mark.needs(*_BACKEND_NEEDS, "store:redis")
async def test_rq_beat_rides_a_redis_restart(infra: Infra, relayed_core_stack: Callable[..., RelayedStack]) -> None:
    _skip_unless_backend(infra, "rq", reason="only the rq variant runs a beat on the stack's Redis")
    # Thirty retries are a window far longer than the outage; the scheduler's own loop
    # has no reconnect.
    stack, redis_relay, _pg_relay = relayed_core_stack(env_overrides={"RQ_RETRY_ATTEMPTS": "30"})
    beat = stack.process("backend-beat")

    offset = _log_size(stack, "backend-beat")
    redis_relay.sever()
    await _wait_for_log(stack, "backend-beat", offset, "Redis connection RQ_REDIS_URL failed (attempt")
    redis_relay.restore()
    wait_relay_ready(redis_relay)

    # Each pass refreshes the scheduler's own key to expire ``interval + 10`` seconds out.
    # Pushed far past that, the key comes back under it only once a pass has run on the
    # restored connection.
    host, port = infra.settings.redis_host_port
    store = redis.Redis(host=host, port=port, db=stack.resources.redis_idx)
    try:
        instances = cast("list[bytes]", store.keys("rq:scheduler_instance:*"))
        assert instances, "the beat registered no scheduler instance"
        for key in instances:
            store.expire(key, 600)

        async def passed() -> bool:
            assert beat.is_running(), beat.log_tail()
            return all(0 < cast("int", store.ttl(key)) <= _BEAT_KEY_TTL for key in instances)

        await wait_for_async(passed, deadline=30.0, message="the beat ran no pass after redis was restored")
    finally:
        store.close()
