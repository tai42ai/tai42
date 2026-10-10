"""Op-level oracles for the checkpoint retention sweep.

The sweep deletes finished threads past the finished horizon on every provider and, on the
``postgres``/``sqlite`` providers, any thread past the waiting horizon; a thread a registered
live-thread filter claims is spared, logged, counted and listed; a deletion that leaves documents
behind is retried and then raised. It also projects as a tool, so it is schedulable through the
existing ``/api/schedules`` create door. The memory and sqlite stores are the kit's real ones.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from prometheus_client import REGISTRY
from tai42_contract.app import tai42_app
from tai42_contract.manifest import ApiToolsConfig
from tai42_kit.llm.checkpoint import ThreadRetention, liveness, register_live_thread_filter
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.operations import OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import checkpoints as checkpoints_ops
from tai42_skeleton.operations import schedules as schedules_ops
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.tools.binding import UnknownToolError

_METADATA: dict[str, Any] = {"source": "input", "step": 0, "parents": {}}


@pytest.fixture(autouse=True)
def _filters_of_this_test(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test registers only the filters it states.
    monkeypatch.setattr(liveness, "_filters", {})


@pytest.fixture
async def store_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Configure the deployment's checkpoint store (waiting 60 min, finished 30 min); return its registry.

    The registry is read after the settings reset, so the sweep and the test share it; every
    resource it opened is closed on the test's loop.
    """
    opened: list[Any] = []

    def _configure(provider: str, conn_string: str | None = None) -> Any:
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", provider)
        if conn_string is not None:
            monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_CONN_STRING", conn_string)
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "60")
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", "30")
        reset_all_settings()
        registry = checkpoint_registry()
        opened.append(registry)
        return registry

    yield _configure
    for registry in opened:
        await registry.close_all()
    reset_all_settings()


async def _put(saver: Any, thread_id: str, *, ts: datetime | None = None) -> None:
    checkpoint = empty_checkpoint()
    if ts is not None:
        checkpoint["ts"] = ts.isoformat()
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    await saver.aput(config, checkpoint, _METADATA, {})


async def _threads(saver: Any) -> set[str]:
    return {tup.config["configurable"]["thread_id"] async for tup in saver.alist(None)}


def _claiming(*thread_ids: str):
    async def _filter(provider: str, conn_string: str | None, candidates: Any) -> set[str]:
        return {thread_id for thread_id in candidates if thread_id in thread_ids}

    return _filter


def _spared_count(horizon: str, owner: str) -> float:
    value = REGISTRY.get_sample_value("tai42_checkpoint_sweep_spared_total", {"horizon": horizon, "owner": owner})
    return value or 0.0


# --------------------------------------------------------------------------- #
# finished horizon (every provider) — the real in-process store and ledger
# --------------------------------------------------------------------------- #
async def test_finished_past_the_horizon_is_deleted_and_forgotten(store_env) -> None:
    registry = store_env("memory")
    saver = await registry.get_checkpointer("memory", None)
    ledger = await registry.ledger("memory", None)
    now = datetime.now(UTC)
    for thread_id in ("done-old", "done-new", "open"):
        await _put(saver, thread_id)
    # A mark carries its due time: due a moment ago goes, due in the future is kept.
    await ledger.mark(["done-old"], now - timedelta(seconds=1))
    await ledger.mark(["done-new"], now + timedelta(minutes=25))

    result = await checkpoints_ops.sweep_checkpoints()

    assert result["finished_swept"] == ["done-old"]
    assert result["waiting_swept"] == []
    assert result["swept_count"] == 1
    assert result["spared"] == []
    assert result["provider"] == "memory"
    assert result["waiting_minutes"] == 60
    assert result["finished_minutes"] == 30
    assert result["skipped"] == "waiting horizon: provider 'memory' keeps threads for the process lifetime"
    assert await _threads(saver) == {"done-new", "open"}
    assert await ledger.finished_before(now + timedelta(minutes=30), limit=10) == ["done-new"]


async def test_a_finished_thread_a_filter_claims_is_spared_logged_counted_and_kept(
    store_env, caplog: pytest.LogCaptureFixture
) -> None:
    registry = store_env("memory")
    saver = await registry.get_checkpointer("memory", None)
    ledger = await registry.ledger("memory", None)
    now = datetime.now(UTC)
    await _put(saver, "claimed")
    await _put(saver, "free")
    await ledger.mark(["claimed", "free"], now - timedelta(hours=2))
    register_live_thread_filter("probe", _claiming("claimed"))
    before = _spared_count("finished", "probe")

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations.checkpoints"):
        result = await checkpoints_ops.sweep_checkpoints()

    assert result["finished_swept"] == ["free"]
    assert result["spared"] == ["claimed"]
    assert await _threads(saver) == {"claimed"}
    assert await ledger.finished_before(now, limit=10) == ["claimed"]  # re-checked by the next sweep
    assert _spared_count("finished", "probe") == before + 1
    assert "thread claimed is past the finished horizon but probe reports it live; kept" in caplog.text


async def test_pages_past_spared_threads_until_the_ledger_is_done(store_env, monkeypatch) -> None:
    registry = store_env("memory")
    monkeypatch.setattr(checkpoints_ops, "_PAGE", 2)
    saver = await registry.get_checkpointer("memory", None)
    ledger = await registry.ledger("memory", None)
    ids = [f"t{i}" for i in range(7)]
    for thread_id in ids:
        await _put(saver, thread_id)
    await ledger.mark(ids, datetime.now(UTC) - timedelta(hours=2))
    register_live_thread_filter("probe", _claiming("t0", "t1", "t4"))

    result = await checkpoints_ops.sweep_checkpoints()

    assert sorted(result["finished_swept"]) == ["t2", "t3", "t5", "t6"]
    assert sorted(result["spared"]) == ["t0", "t1", "t4"]


async def test_a_filter_raise_fails_the_operation(store_env) -> None:
    registry = store_env("memory")
    ledger = await registry.ledger("memory", None)
    await ledger.mark(["t"], datetime.now(UTC) - timedelta(hours=2))

    async def _broken(provider: str, conn_string: str | None, candidates: Any) -> set[str]:
        raise RuntimeError("liveness store unreachable")

    register_live_thread_filter("broken", _broken)
    with pytest.raises(RuntimeError, match="liveness store unreachable"):
        await checkpoints_ops.sweep_checkpoints()


# --------------------------------------------------------------------------- #
# waiting horizon — sqlite (the real store) and postgres (the adapter query)
# --------------------------------------------------------------------------- #
async def test_sqlite_waiting_past_the_horizon_is_deleted_unless_claimed(store_env, tmp_path) -> None:
    pytest.importorskip("aiosqlite")
    db = str(tmp_path / "checkpoints.db")
    registry = store_env("sqlite", db)
    saver = await registry.get_checkpointer("sqlite", db)
    now = datetime.now(UTC)
    await _put(saver, "stale", ts=now - timedelta(hours=3))
    await _put(saver, "stale", ts=now - timedelta(hours=2))
    await _put(saver, "parked", ts=now - timedelta(hours=2))
    await _put(saver, "fresh", ts=now - timedelta(minutes=5))
    await _put(saver, "declared-short", ts=now - timedelta(minutes=5))
    await _put(saver, "declared-long", ts=now - timedelta(minutes=50))
    ledger = await registry.ledger("sqlite", db)
    await ledger.start(["declared-short"], ThreadRetention(waiting_minutes=2, finished_minutes=1))
    await ledger.start(["declared-long"], ThreadRetention(waiting_minutes=55, finished_minutes=1))
    register_live_thread_filter("probe", _claiming("parked"))

    result = await checkpoints_ops.sweep_checkpoints()

    assert result["waiting_swept"] == ["declared-short", "stale"]
    assert result["spared"] == ["parked"]
    assert result["skipped"] is None
    assert await _threads(saver) == {"parked", "fresh", "declared-long"}
    assert await ledger.declared_waiting(["declared-short", "declared-long"]) == {"declared-long": 55}


class _Store:
    """A checkpoint store over ``{thread_id: rounds_left}``: each delete removes one round of documents."""

    def __init__(self, rounds: dict[str, int]) -> None:
        self.rounds = rounds
        self.deletes: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deletes.append(thread_id)
        if self.rounds.get(thread_id, 0) > 0:
            self.rounds[thread_id] -= 1
        if self.rounds.get(thread_id) == 0:
            self.rounds.pop(thread_id)

    async def alist(self, config: Any, *, limit: int | None = None) -> AsyncIterator[Any]:
        if config["configurable"]["thread_id"] in self.rounds:
            yield SimpleNamespace(config=config)


class _Ledger:
    def __init__(self, finished: list[str] | None = None) -> None:
        self.finished = list(finished or [])
        self.forgotten: list[str] = []
        self.cutoffs: list[datetime] = []

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        self.cutoffs.append(cutoff)
        return self.finished[:limit]

    async def forget(self, thread_ids: list[str]) -> None:
        self.forgotten.extend(thread_ids)
        self.finished = [t for t in self.finished if t not in thread_ids]


def _install_store(monkeypatch: pytest.MonkeyPatch, store_env: Any, provider: str, store: _Store, ledger: _Ledger):
    store_env(provider)
    resource = SimpleNamespace(provider=provider, handle="pool", ledger=ledger)

    async def _resource(provider: str, conn_string: str | None) -> Any:
        return resource

    async def _get_checkpointer(provider: str, conn_string: str | None) -> Any:
        return store

    monkeypatch.setattr(
        checkpoints_ops,
        "checkpoint_registry",
        lambda: SimpleNamespace(resource=_resource, get_checkpointer=_get_checkpointer),
    )


async def test_postgres_waiting_past_the_horizon_is_deleted_unless_claimed(monkeypatch, store_env) -> None:
    from tai42_kit.llm.checkpoint import postgres_store

    store = _Store({"stale": 1, "parked": 1, "fresh": 1})
    ledger = _Ledger()
    _install_store(monkeypatch, store_env, "postgres", store, ledger)
    calls: list[tuple[datetime, int]] = []

    async def _stale(pool: Any, *, now: datetime, default_minutes: int) -> list[str]:
        assert pool == "pool"
        calls.append((now, default_minutes))
        return ["parked", "stale"]

    monkeypatch.setattr(postgres_store, "stale_threads", _stale)
    register_live_thread_filter("probe", _claiming("parked"))

    result = await checkpoints_ops.sweep_checkpoints()

    assert result["waiting_swept"] == ["stale"]
    assert result["spared"] == ["parked"]
    assert set(store.rounds) == {"parked", "fresh"}
    assert ledger.forgotten == ["stale"]
    [(swept_at, default_minutes)] = calls
    assert default_minutes == 60
    assert datetime.now(UTC) - timedelta(minutes=1) < swept_at <= datetime.now(UTC)


async def test_redis_waiting_is_left_to_the_key_ttl(monkeypatch, store_env) -> None:
    store = _Store({"waiting": 1, "done": 1})
    ledger = _Ledger(["done"])
    _install_store(monkeypatch, store_env, "redis", store, ledger)

    result = await checkpoints_ops.sweep_checkpoints()

    assert result["finished_swept"] == ["done"]
    assert result["waiting_swept"] == []
    assert result["skipped"] == "waiting horizon: provider 'redis' expires threads by their key TTL"
    assert set(store.rounds) == {"waiting"}
    # The ledger stores each mark's due time, so the sweep asks for what is due now.
    assert datetime.now(UTC) - timedelta(minutes=1) < ledger.cutoffs[0] <= datetime.now(UTC)


async def test_a_thread_that_survives_a_delete_round_is_deleted_on_the_next(monkeypatch, store_env) -> None:
    store = _Store({"big": 3})
    _install_store(monkeypatch, store_env, "redis", store, _Ledger(["big"]))

    result = await checkpoints_ops.sweep_checkpoints()

    assert result["finished_swept"] == ["big"]
    assert store.deletes == ["big", "big", "big"]


async def test_a_thread_that_survives_every_delete_round_raises(monkeypatch, store_env) -> None:
    store = _Store({"stuck": 10_000})
    ledger = _Ledger(["stuck"])
    _install_store(monkeypatch, store_env, "redis", store, ledger)

    with pytest.raises(checkpoints_ops.CheckpointSweepError, match="thread stuck still holds checkpoints after 100"):
        await checkpoints_ops.sweep_checkpoints()
    assert len(store.deletes) == 100
    assert ledger.forgotten == []


# --------------------------------------------------------------------------- #
# projection and scheduling
# --------------------------------------------------------------------------- #
def test_projects_as_a_schedulable_tool() -> None:
    reg = OperationRegistry()
    reg.register(operation_metadata_of(checkpoints_ops.sweep_checkpoints))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force: bool, name: str, tags: set, annotations: object) -> Any:
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    app = SimpleNamespace(tools=_Rec())
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    assert "sweep_checkpoints" in names  # projected → dispatchable by name
    assert app.tools.registered["sweep_checkpoints"]["annotations"].destructiveHint is True


async def test_schedulable_via_create_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    # A scheduler-capable backend (marker tools present) plus the projected sweep tool and
    # its ``schedule_task`` vehicle: the create-schedule door translates a cadence onto that
    # branch, so the sweep is schedulable.
    class _FakeTools:
        def __init__(self, registered: set[str]) -> None:
            self._registered = registered

        async def get_tools(self) -> dict:
            return {name: SimpleNamespace(name=name) for name in self._registered}

        async def run_tool(self, key: str, arguments: dict) -> object:
            if key not in self._registered:
                raise UnknownToolError(key)
            return {"scheduled": key, "arguments": arguments}

    fake = _FakeTools(
        {"backend_list_schedules", "backend_delete_schedule", "sweep_checkpoints", "sweep_checkpoints_schedule_task"}
    )
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=fake))

    # The submitted-tool authorization runs the full HTTP-edge decision against the live
    # caller; there is no caller identity in this op-level unit, so stub it to an allow —
    # the dispatch-by-name path is what this test pins, not the authz seam (covered by
    # ``tests/operations/test_schedules_ops.py``).
    async def _allow(tool_name: str, arguments: dict) -> None:
        return None

    monkeypatch.setattr(schedules_ops, "authorize_submitted_tool", _allow)

    result = await schedules_ops.create_schedule("sweep_checkpoints", {}, {"cron": "0 3 * * *"})
    # The friendly cron is translated onto the sweep's ``schedule_task`` vehicle: the branch
    # is dispatched with the cadence as ``backend_schedule`` under a derived name.
    assert result["scheduled"] == "sweep_checkpoints_schedule_task"
    assert result["arguments"]["backend_schedule"] == "0 3 * * *"
    assert result["arguments"]["backend_schedule_name"].startswith("sweep_checkpoints_")
