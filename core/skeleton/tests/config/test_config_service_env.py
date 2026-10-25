"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the env doors:
``apply_env_change`` (an env override) and ``apply_env_and_change`` (the combined
env-write + manifest-mutate seam), plus connector-secret stickiness across a dropped oauth
connector.

The combined-op tests assert env-first/manifest-second ordering, the no-rollback orphan
contract, the external-store 409 replay writing the env exactly once, and the process-wide
lock serializing a concurrent secret-marks append; the stickiness tests assert a leaving
``client_secret_env`` name is folded into the stored marks so its value stays masked.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any

import pytest

# Imported for its registration side effect: the env doors classify a change by the live
# registry, and the backend group's ``BACKEND_MANIFEST_KEY`` is recycle-class.
import tai42_skeleton.backend.settings  # noqa: F401
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleError, RecycleReport, RecycleRow, RecycleStop
from tai42_skeleton.config.service import OrphanEnvWriteError

from .._fakes.bus import FakeBus
from .._fakes.combo_check_app import booted_check_app, clean_server
from .fake_pipeline import (
    ComboChecks,
    FakeConfigStore,
    FakeReloadAdmin,
    RetryingConfigStore,
    _no_bus,
    _none_descriptor,
    _oauth_descriptor,
    _orchestrate_spy,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# ---------------------------------------------------------------------------
# apply_env_change
# ---------------------------------------------------------------------------


async def test_apply_env_change_writes_reloads_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, bus = _service(store)

    result = await service.apply_env_change({"NEW_KEY": "v"})

    assert store.env_writes == [{"NEW_KEY": "v"}]
    assert store.env == {"EXISTING": "1", "NEW_KEY": "v"}
    assert admin.calls == 1
    assert bus.publish_calls[0] == ({"op": "reload_config"}, None, bus.publish_calls[0][2])
    # An env change touches no manifest document.
    assert result.document is None
    assert result.local == {"status": "ok", "env_keys": 0}


async def test_a_derived_env_change_derives_from_a_write_that_landed_while_it_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The combined op holds the write exclusion across its yielding prepare; the derived
    # change queued behind it derives its marks from the env that op wrote, so both stand.
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service_a, _admin_a, _bus_a = _service(store)
    service_b, admin_b, _bus_b = _service(store)

    def _append_key_b(stored: dict[str, str]) -> dict[str, str]:
        marks = [mark for mark in stored.get("TAI_ENV_SECRET_KEYS", "").split(",") if mark]
        return {"TAI_ENV_SECRET_KEYS": ",".join([*marks, "KEY_B"])}

    await asyncio.wait_for(
        asyncio.gather(
            service_a.apply_env_and_change(_marks_appending_prepare("KEY_A")),
            service_b.apply_derived_env_change(_append_key_b),
        ),
        10,
    )

    assert store.env["TAI_ENV_SECRET_KEYS"] == "KEY_A,KEY_B"
    assert admin_b.calls == 1


async def test_a_derived_env_change_whose_derive_raises_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, _bus = _service(store)

    def _refuse(_stored: dict[str, str]) -> dict[str, str]:
        raise RuntimeError("cannot derive")

    with pytest.raises(RuntimeError, match="cannot derive"):
        await service.apply_derived_env_change(_refuse)

    assert store.env_writes == []
    assert admin.calls == 0


async def test_apply_env_change_refuses_a_recycle_class_key_on_a_bare_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No supervisor can respawn a worker under the new value, so the env door refuses it
    # upfront, naming the key, as the profile door does — never written and reported applied
    # while the running bus keeps its boot value.
    _with_bus(monkeypatch)
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    store = FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_HEARTBEAT_TTL": "15"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match=r"bare.*TAI_BUS_HEARTBEAT_TTL") as refused:
        await service.apply_env_change({"TAI_BUS_HEARTBEAT_TTL": "7"})

    # The refusal names the way out that fits an env change, beside the supervised one.
    assert "Set them in the deployment's environment (or the stored .env) and restart the server" in str(refused.value)
    assert "or run under a supervised deployment, where a recycle carries them" in str(refused.value)
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_env_change_refuses_the_bus_url_on_every_deployment_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bus URL reaches the worker bus itself: a recycle can never carry it, so it is
    # refused on a supervised shape too, naming the key.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BUS_REDIS_URL": "redis://localhost:6379/5"})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_env_change_refuses_removing_a_recycle_class_key_on_a_bare_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    store = FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_HEARTBEAT_TTL": "15"})
    service, admin, _bus = _service(store)

    with pytest.raises(ValueError, match=r"bare.*TAI_BUS_HEARTBEAT_TTL"):
        await service.apply_env_change({"TAI_BUS_HEARTBEAT_TTL": ""})

    assert store.env_writes == []
    assert admin.calls == 0


async def test_apply_env_change_carries_an_unchanged_recycle_class_key_on_a_bare_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read-modify-write round-trip re-sends every stored key: an unchanged recycle-class
    # value is no change, so the write beside it lands.
    _with_bus(monkeypatch)
    monkeypatch.delenv("TAI_SUPERVISED", raising=False)
    store = FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_HEARTBEAT_TTL": "15"})
    service, admin, _bus = _service(store)

    await service.apply_env_change({"TAI_BUS_HEARTBEAT_TTL": "15", "NEW_KEY": "v"})

    assert store.env == {"TAI_BUS_HEARTBEAT_TTL": "15", "NEW_KEY": "v"}
    assert admin.calls == 1


class _OrderedAdmin(FakeReloadAdmin):
    """A reload admin that records when the local reload ran, beside the bus's census reads."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def reload_config(self) -> dict[str, Any]:
        self.calls += 1
        self._events.append("reload")
        return {"status": "ok", "env_keys": 1, "awaiting_respawn": []}


class _OrderedBus(FakeBus):
    """A fake bus that records each census read and carries one backend sibling."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(origin="serve-writer", remotes=["serve-b"])
        self._events = events

    async def census(self) -> list[Any]:
        self._events.append("census")
        rows = await super().census()
        backend = rows[-1].model_copy(update={"name": "backend-1", "kind": WorkerKind.backend, "generation": 3})
        return [*rows, backend]


def _supervised(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_SUPERVISED", "harness")


async def test_apply_env_change_rolls_a_recycle_for_a_recycle_class_key_on_a_supervised_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _supervised(monkeypatch)
    events: list[str] = []
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, admin=_OrderedAdmin(events), bus=_OrderedBus(events), orchestrate=orch)

    result = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert store.env == {"BACKEND_MANIFEST_KEY": "NEW"}
    assert seen["excluded_name"] == "serve-writer"
    assert seen["applier_generation"] == 1
    assert seen["applier_self_deferred"] is True
    assert [kind.value for kind in seen["target_kinds"]] == ["backend", "serve"]
    # The targets are pinned from a census read BEFORE the local reload.
    assert seen["expected"] == {"serve-b": (WorkerKind.serve, 1), "backend-1": (WorkerKind.backend, 3)}
    assert events.index("census") < events.index("reload")
    assert result.recycle is not None
    assert [row.name for row in result.recycle.rows] == ["serve-b"]
    assert result.self_exit_armed is True


async def test_apply_env_change_of_hot_keys_only_rolls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=FakeBus(origin="serve-writer"), orchestrate=orch)

    result = await service.apply_env_change({"NEW_KEY": "v", "BACKEND_MANIFEST_KEY": "OLD"})

    assert seen == {}
    assert result.recycle is None
    assert result.self_exit_armed is False


async def test_apply_env_and_change_rolls_a_recycle_for_a_recycle_class_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch)

    result = await service.apply_env_and_change(
        _prepare({"BACKEND_MANIFEST_KEY": "NEW", "GH": "s"}, _secret_marker_mutator("GH"))
    )

    assert seen["excluded_name"] == "serve-writer"
    assert result.recycle is not None
    assert result.self_exit_armed is True


async def test_a_stopped_env_change_roll_returns_the_partial_report_and_arms_no_self_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    detail = "old life still present after 30s"
    partial = RecycleReport(
        rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=TIMED_OUT, detail=detail)],
        stopped=RecycleStop(kind="serve", name="serve-b", detail=detail),
    )
    orch, _seen = _orchestrate_spy(raises=RecycleError(f"recycle: {detail}", partial))
    service, _admin, _bus = _service(store, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch)

    with caplog.at_level(logging.ERROR):
        result = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert result.recycle is partial
    assert result.self_exit_armed is False
    assert "recycle for this env change did not converge" in caplog.text
    assert store.env == {"BACKEND_MANIFEST_KEY": "NEW"}


async def test_an_env_change_roll_raising_with_no_recorded_stop_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, _seen = _orchestrate_spy(raises=RecycleError("recycle: contract violation", RecycleReport()))
    service, _admin, _bus = _service(store, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch)

    with pytest.raises(RecycleError, match="contract violation"):
        await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})


_MOVED = {"status": "ok", "env_keys": 1, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}


class _MovedBus(FakeBus):
    """A fake bus whose every sibling answers the reload with the fact that its env moved."""

    async def publish(self, op: FleetOp, targets: Any, local: Any, *, expected_at_start: Any = None) -> Any:
        from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerResult

        report = await super().publish(op, targets, local, expected_at_start=expected_at_start)
        rows = [
            row
            if row.name == self.identity.name
            else WorkerResult(name=row.name, outcome=OpOutcome.applied, payload=_MOVED)
            for row in report.results
        ]
        return FleetResult(op=report.op, results=rows)


def _moved_admin() -> Any:
    from .fake_pipeline import FakeReloadAdmin

    return FakeReloadAdmin(result=dict(_MOVED))


async def test_a_converged_env_change_roll_leaves_nobody_awaiting_a_respawn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, _seen = _orchestrate_spy()
    service, _admin, _bus = _service(
        store, admin=_moved_admin(), bus=_MovedBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch
    )

    with caplog.at_level(logging.ERROR):
        result = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert result.awaiting_respawn == []
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


async def test_a_stopped_env_change_roll_leaves_the_unreached_and_the_writer_awaiting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    partial = RecycleReport(
        rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=TIMED_OUT, detail="slow")],
        stopped=RecycleStop(kind="serve", name="serve-b", detail="slow"),
    )
    orch, _seen = _orchestrate_spy(raises=RecycleError("recycle: slow", partial))
    bus = _MovedBus(origin="serve-writer", remotes=["serve-b", "serve-c"])
    service, _admin, _bus = _service(store, admin=_moved_admin(), bus=bus, orchestrate=orch)

    with caplog.at_level(logging.ERROR):
        result = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert [(w.name, w.keys) for w in result.awaiting_respawn] == [
        ("serve-b", ["BACKEND_MANIFEST_KEY"]),
        ("serve-c", ["BACKEND_MANIFEST_KEY"]),
        ("serve-writer", ["BACKEND_MANIFEST_KEY"]),
    ]
    awaiting_lines = [r.getMessage() for r in caplog.records if "awaiting a respawn" in r.getMessage()]
    assert len(awaiting_lines) == 1
    assert "serve-b (BACKEND_MANIFEST_KEY), serve-c (BACKEND_MANIFEST_KEY), serve-writer" in awaiting_lines[0]


async def test_a_hot_change_names_a_sibling_whose_env_moved_out_of_band(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "NEW"})
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=_MovedBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch)

    with caplog.at_level(logging.ERROR):
        result = await service.apply_env_change({"LOG_LEVEL": "debug"})

    assert seen == {}
    assert [(w.name, w.keys) for w in result.awaiting_respawn] == [("serve-b", ["BACKEND_MANIFEST_KEY"])]
    assert len([r for r in caplog.records if "awaiting a respawn" in r.getMessage()]) == 1


class _ForkChildBus(_MovedBus):
    """A forked job child's bus: a non-member acting for its member parent ``backend-1``.

    The census carries the member parent and a serve sibling, never the child; the parent
    answers the published reload as a sibling, stating the moved key as every reload does.
    """

    def __init__(self) -> None:
        super().__init__(origin="backend-1/fork-7", remotes=["backend-1", "serve-1"])
        self._identity = WorkerIdentity(
            name="backend-1/fork-7", kind=WorkerKind.backend, pid=7, generation=0, member=False
        )
        self._member = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=1, generation=3)

    async def census(self) -> list[Any]:
        rows = await super().census()
        backend = rows[1].model_copy(update={"kind": WorkerKind.backend, "generation": 3})
        return [backend, rows[2]]


async def test_an_env_change_in_a_forked_job_child_applies_nothing_locally_and_acts_for_its_member(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A tool door run inside a forked backend job (a schedule fire, a background submit) writes
    # the env from a non-member child. The child has no serving surface to converge and the bus
    # takes no self entry from it: it reloads nothing, publishes with no local result, and the
    # member parent converges as a sibling. The writer is that member: excluded from the roll at
    # its own generation, reported self-deferred, and never left awaiting a respawn.
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-1", kind="serve", generation_before=1, status=RECYCLED)])
    )
    bus = _ForkChildBus()
    service, admin, _bus = _service(store, admin=_moved_admin(), bus=bus, orchestrate=orch)

    with caplog.at_level(logging.ERROR):
        result = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert store.env == {"BACKEND_MANIFEST_KEY": "NEW"}
    assert admin.calls == 0
    assert [call[2] for call in bus.publish_calls] == [None]
    assert seen["excluded_name"] == "backend-1"
    assert seen["applier_generation"] == 3
    assert seen["expected"] == {"serve-1": (WorkerKind.serve, 1)}
    assert result.self_identity == bus.member_identity
    assert result.self_exit_armed is True
    assert result.local == {"status": "ok", "env_keys": 1}
    assert {row.name for row in result.fleet.results} == {"backend-1", "serve-1"}
    assert result.awaiting_respawn == []
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


class _CountingMovedBus(_MovedBus):
    """A moved-env bus that counts the census reads the recycle pin takes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.census_reads = 0

    async def census(self) -> list[Any]:
        self.census_reads += 1
        return await super().census()


def _counting(
    spy: Callable[..., Awaitable[RecycleReport]],
) -> tuple[Callable[..., Awaitable[RecycleReport]], list[int]]:
    calls: list[int] = []

    async def counted(*args: Any, **kwargs: Any) -> RecycleReport:
        calls.append(1)
        return await spy(*args, **kwargs)

    return counted, calls


async def test_a_reapply_that_continues_a_converged_change_carries_its_roll_and_rolls_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    spy, _seen = _orchestrate_spy()
    orch, rolls = _counting(spy)
    bus = _CountingMovedBus(origin="serve-writer", remotes=["serve-b"])
    service, _admin, _bus = _service(store, admin=_moved_admin(), bus=bus, orchestrate=orch)
    prior = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})
    census_after_change = bus.census_reads

    with caplog.at_level(logging.ERROR):
        result = await service.reapply_manifest(continues=prior)

    assert rolls == [1]
    assert bus.census_reads == census_after_change
    assert result.recycle is prior.recycle
    assert result.self_identity == prior.self_identity
    assert result.self_exit_armed is True
    assert result.awaiting_respawn == []
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


async def test_a_reapply_that_continues_a_stopped_change_names_the_unreached_and_the_writer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    partial = RecycleReport(
        rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=TIMED_OUT, detail="slow")],
        stopped=RecycleStop(kind="serve", name="serve-b", detail="slow"),
    )
    orch, _seen = _orchestrate_spy(raises=RecycleError("recycle: slow", partial))
    bus = _MovedBus(origin="serve-writer", remotes=["serve-b", "serve-c"])
    service, _admin, _bus = _service(store, admin=_moved_admin(), bus=bus, orchestrate=orch)
    prior = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        result = await service.reapply_manifest(continues=prior)

    assert result.recycle is partial
    assert result.self_exit_armed is False
    assert [w.name for w in result.awaiting_respawn] == ["serve-b", "serve-c", "serve-writer"]
    assert len([r for r in caplog.records if "awaiting a respawn" in r.getMessage()]) == 1


async def test_a_reapply_given_both_a_continued_change_and_keys_of_its_own_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, _seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch)
    prior = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    with pytest.raises(ValueError, match="rolls no recycle keys of its own"):
        await service.reapply_manifest(continues=prior, recycle_keys=["BACKEND_MANIFEST_KEY"])


async def test_a_local_reload_failure_carries_the_change_s_unrolled_recycle_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    from .fake_pipeline import FakeReloadAdmin

    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    orch, seen = _orchestrate_spy()
    admin = FakeReloadAdmin(raise_reload=RuntimeError("boom"))
    service, _admin, _bus = _service(
        store, admin=admin, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch
    )

    with pytest.raises(FleetBroadcastError) as raised:
        await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert raised.value.unrolled_recycle_keys == ("BACKEND_MANIFEST_KEY",)
    assert seen == {}


async def test_a_reapply_handed_a_change_s_unrolled_keys_rolls_their_recycle(monkeypatch: pytest.MonkeyPatch) -> None:
    _supervised(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "NEW"})
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=FakeBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=orch)

    result = await service.reapply_manifest(recycle_keys=["BACKEND_MANIFEST_KEY"])

    assert seen["excluded_name"] == "serve-writer"
    assert result.recycle is not None
    assert result.self_exit_armed is True


# ---------------------------------------------------------------------------
# apply_env_and_change (combined env-write + manifest-mutate)
# ---------------------------------------------------------------------------


async def test_apply_env_and_change_refuses_the_bus_url_on_every_deployment_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"})
    service, admin, _bus = _service(store)

    with pytest.raises(ValueError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_and_change(
            _prepare({"TAI_BUS_REDIS_URL": "redis://localhost:6379/5"}, _secret_marker_mutator("TAI_BUS_REDIS_URL"))
        )

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0


def _secret_marker_mutator(var: str) -> Callable[[dict[str, Any]], None]:
    """A pure mutator that writes an ``!ENV ${var}`` marker into a fresh MCP entry —
    the shape ``set_mcp_secret_env`` produces (re-runnable, external-store-409-replay-safe)."""

    def mutator(document: dict[str, Any]) -> None:
        document["mcp"] = [
            {"title": "gh", "config": {"url": "https://x", "headers": {"Authorization": f"!ENV ${{{var}}}"}}}
        ]

    return mutator


def _prepare(
    changes: dict[str, str], mutator: Callable[[dict[str, Any]], None]
) -> Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]]:
    """Wrap fixed ``(changes, mutator)`` as the async ``prepare`` callback
    :meth:`ConfigService.apply_env_and_change` now takes — the real op derives these from the
    lock-held stored-env read; a test not exercising derivation returns them verbatim."""

    async def _p(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return changes, mutator

    return _p


async def test_apply_env_and_change_writes_env_and_mutates_manifest_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, _bus = _service(store)

    result = await service.apply_env_and_change(_prepare({"GH": "the-secret"}, _secret_marker_mutator("GH")))

    # Env write + manifest mutate both landed, consistently.
    assert store.env == {"EXISTING": "1", "GH": "the-secret"}
    assert store.manifest["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GH}"
    # The persisted manifest keeps the MARKER (no resolved secret bakes to disk).
    assert store.persisted[-1]["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GH}"
    assert admin.calls == 1
    assert result.document is not None


async def test_apply_env_and_change_manifest_failure_leaves_orphan_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # partial-failure contract: env-FIRST/manifest-SECOND; a manifest
    # persist failure AFTER the env write does NOT roll the env back — the env write STANDS as
    # an inert, re-runnable orphan — and the op raises loudly (OrphanEnvWriteError) NAMING the
    # orphan env key AND the manifest pointer, stating the env write stands / re-run.
    _no_bus(monkeypatch)

    class FailingMutateStore(FakeConfigStore):
        def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
            raise RuntimeError("manifest persist boom")

    store = FailingMutateStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, _bus = _service(store)

    with pytest.raises(OrphanEnvWriteError) as excinfo:
        await service.apply_env_and_change(
            _prepare({"GH": "the-secret"}, _secret_marker_mutator("GH")),
            manifest_pointer="mcp/0/config/headers/Authorization",
        )

    message = str(excinfo.value)
    assert "GH" in message  # names the now-orphan env key
    assert "mcp/0/config/headers/Authorization" in message  # names the manifest pointer
    assert "stands" in message.lower()  # env write stands
    assert "re-run" in message.lower()  # re-runnable
    # The original persist failure is chained, not swallowed.
    assert isinstance(excinfo.value.__cause__, RuntimeError)

    # NO rollback: the env write STANDS — the orphan key REMAINS in the store (a single
    # write_env, no compensating replace_env), and nothing persisted / reloaded.
    assert store.env == {"EXISTING": "1", "GH": "the-secret"}
    assert store.env_writes == [{"GH": "the-secret"}]  # only the write_env; no rollback restore
    assert store.persisted == []
    assert admin.calls == 0


async def test_apply_env_and_change_external_store_409_replay_writes_env_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # An external store's optimistic-concurrency retry re-runs the manifest mutator (RetryingConfigStore
    # discards a first attempt). The env write is OUTSIDE that replayed span, so it happens
    # exactly once — env + manifest stay consistent on a 409 replay, no double env write.
    _no_bus(monkeypatch)
    store = RetryingConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    await service.apply_env_and_change(_prepare({"GH": "the-secret"}, _secret_marker_mutator("GH")))

    assert store.env_writes == [{"GH": "the-secret"}]  # single env write despite the manifest replay
    assert store.env == {"GH": "the-secret"}
    assert store.manifest["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GH}"


async def test_apply_env_and_change_refuses_x_band_env_key_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match="TAI_RUN_MODE"):
        await service.apply_env_and_change(
            _prepare({"GH": "the-secret", "TAI_RUN_MODE": "spoof"}, _secret_marker_mutator("GH"))
        )

    # X-band refused up front — neither store was touched.
    assert store.env == {"EXISTING": "1"}
    assert store.env_writes == []
    assert store.persisted == []


async def test_apply_env_and_change_refuses_dangling_marker_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mutator writes an `!ENV ${MISSING}` marker but the env changes do not supply
    # MISSING → dangling, refused before any write (naming the var).
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match="MISSING"):
        await service.apply_env_and_change(_prepare({"OTHER": "v"}, _secret_marker_mutator("MISSING")))

    assert store.env_writes == []
    assert store.persisted == []


def _marks_appending_prepare(
    key: str,
) -> Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]]:
    """A ``prepare`` that mirrors the read→append→write hazard: it reads the stored marks,
    YIELDS (``await asyncio.sleep(0)``) to force a concurrent op to try to interleave, THEN
    appends its own key. Under the config-write lock the yield cannot let the other op read stale
    marks; without it, both would read the same marks and the second write would lose the first."""

    async def _p(stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        existing = [m for m in stored.get("TAI_ENV_SECRET_KEYS", "").split(",") if m]
        await asyncio.sleep(0)  # the interleave point the lock must cover
        marks = list(dict.fromkeys([*existing, key]))
        changes = {key: f"secret-{key}", "TAI_ENV_SECRET_KEYS": ",".join(marks)}
        return changes, _secret_marker_mutator(key)

    return _p


async def test_apply_env_and_change_lock_serializes_concurrent_marks_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two concurrent combined ops each APPEND their secret mark. Each prepare reads the
    # stored marks, YIELDS to force interleave, then appends — so without serialization both
    # read the same marks and the second write clobbers the first (a lost append). The
    # process-wide (CLASS-level) config-write lock must serialize the read→write span so BOTH
    # marks survive. Two separate ConfigService instances (as ``from_app`` builds per call)
    # over ONE shared store prove the lock is shared across instances, not per-instance.
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service_a, admin_a, _bus_a = _service(store)
    service_b, admin_b, _bus_b = _service(store)

    await asyncio.gather(
        service_a.apply_env_and_change(_marks_appending_prepare("KEY_A")),
        service_b.apply_env_and_change(_marks_appending_prepare("KEY_B")),
    )

    marks = store.env["TAI_ENV_SECRET_KEYS"].split(",")
    assert "KEY_A" in marks, f"KEY_A's mark was lost to the concurrent append: {marks}"
    assert "KEY_B" in marks, f"KEY_B's mark was lost to the concurrent append: {marks}"
    # Both secret values landed too, and each op ran its own reload (lock released before it).
    assert store.env["KEY_A"] == "secret-KEY_A"
    assert store.env["KEY_B"] == "secret-KEY_B"
    assert admin_a.calls == 1
    assert admin_b.calls == 1


# ---------------------------------------------------------------------------
# Connector-secret stickiness across removal
# ---------------------------------------------------------------------------


async def test_apply_change_dropping_oauth_connector_persists_its_secret_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"connectors": [_oauth_descriptor("acme")]}, env={})
    service, _admin, _bus = _service(store)

    def drop(document: dict[str, Any]) -> None:
        document["connectors"] = []

    await service.apply_change(drop)

    # The connector is gone, but its client_secret_env name is now in the stored marks so
    # the orphaned value keeps its mask (the value itself is never deleted).
    assert store.manifest["connectors"] == []
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_update_to_none_kind_keeps_the_dropped_secret_name_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An oauth->none update keeps the connector id but drops its client_secret_env NAME; the
    # name must still be persisted into the marks (a set difference of NAMES, not of ids).
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"connectors": [_oauth_descriptor("acme")]}, env={})
    service, _admin, _bus = _service(store)

    def to_none(document: dict[str, Any]) -> None:
        document["connectors"] = [_none_descriptor("acme")]

    await service.apply_change(to_none)

    assert store.manifest["connectors"][0]["kind"] == "none"
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_apply_change_not_dropping_a_connector_writes_no_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manifest change that drops NO oauth connector never touches the marks var (no env
    # write) — the plain apply_change path.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    def add(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    await service.apply_change(add)
    assert store.env_writes == []
    assert "TAI_ENV_SECRET_KEYS" not in store.env


async def test_apply_replace_dropping_connector_persists_mark_and_deletes_omitted_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hand `manifest replace` that drops an oauth connector AND omits another top-level
    # section: the leaving secret name is persisted, and the omitted section is DELETED (the
    # delegated replace-as-mutator is clear()+update(), never a merge).
    _with_bus(monkeypatch)
    store = FakeConfigStore(
        manifest={"connectors": [_oauth_descriptor("acme")], "mcp": [{"title": "srv", "config": {"url": "http://x"}}]},
        env={},
    )
    service, _admin, _bus = _service(store)

    # The replacement carries neither the connector nor the mcp section.
    await service.apply_replace({"user_tools": []})

    assert "connectors" not in store.manifest or store.manifest["connectors"] == []
    assert "mcp" not in store.manifest  # the omitted section was deleted, not merged
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_leaving_mark_written_only_when_it_grows_the_stored_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The leaving name is ALREADY in the stored marks, so dropping the connector must NOT
    # rewrite the marks var (no growth).
    _with_bus(monkeypatch)
    store = FakeConfigStore(
        manifest={"connectors": [_oauth_descriptor("acme")]},
        env={"TAI_ENV_SECRET_KEYS": "ACME_CLIENT_SECRET"},
    )
    service, _admin, _bus = _service(store)

    def drop(document: dict[str, Any]) -> None:
        document["connectors"] = []

    await service.apply_change(drop)
    # No env write carried the marks var (the set did not grow); the value stands.
    assert all("TAI_ENV_SECRET_KEYS" not in write for write in store.env_writes)
    assert store.env["TAI_ENV_SECRET_KEYS"] == "ACME_CLIENT_SECRET"


# ---------------------------------------------------------------------------
# owned-prefix audit on every env writer
# ---------------------------------------------------------------------------


async def test_env_write_setting_an_unknown_owned_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import UnknownOwnedSettingError

    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, _bus = _service(store)

    with pytest.raises(UnknownOwnedSettingError, match=r"^env write: .*INTERACTIONS_TYPO \(prefix INTERACTIONS_\)"):
        await service.apply_env_change({"INTERACTIONS_TYPO": "x", "INTERACTIONS_KEY_PREFIX": "k:"})

    assert store.env_writes == []
    assert admin.calls == 0


async def test_env_write_deleting_a_stored_unknown_owned_name_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"INTERACTIONS_TYPO": "x"})
    service, _admin, _bus = _service(store)

    await service.apply_env_change({"INTERACTIONS_TYPO": ""})

    # The deleting write passes the audit and lands (the real managers drop an emptied key).
    assert store.env_writes == [{"INTERACTIONS_TYPO": ""}]


async def test_combined_env_write_setting_an_unknown_owned_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import UnknownOwnedSettingError

    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    with pytest.raises(UnknownOwnedSettingError, match="CHANNELS_TYPO"):
        await service.apply_env_and_change(_prepare({"CHANNELS_TYPO": "x"}, _secret_marker_mutator("CHANNELS_TYPO")))

    assert store.env_writes == []
    assert store.persisted == []


def test_profile_replace_setting_an_unknown_owned_name_is_refused() -> None:
    from tai42_kit.settings import UnknownOwnedSettingError

    store = FakeConfigStore(manifest={}, env={})
    service, _admin, _bus = _service(store)

    with pytest.raises(UnknownOwnedSettingError, match="ACCESS_CONTROL_ENABEL"):
        service._validate_replace({"ACCESS_CONTROL_ENABEL": "true"})


def test_profile_replace_omitting_a_stored_unknown_owned_name_passes() -> None:
    store = FakeConfigStore(manifest={}, env={"INTERACTIONS_TYPO": "x"})
    service, _admin, _bus = _service(store)

    service._validate_replace({"INTERACTIONS_KEY_PREFIX": "k:"})


# ---------------------------------------------------------------------------
# Extension combos — an env change re-checks every combo with its settings resolved anew
# ---------------------------------------------------------------------------

_MARKED_COMBO: dict[str, Any] = {
    "tools": [
        {
            "title": "a",
            "module": "tests.app._fixtures.tools_check",
            "include": ["echo"],
            "extensions": {"echo": [[{"name": "refusing", "config": {"label": "!ENV ${EXT_L}"}}]]},
        }
    ]
}

_REFUSED_PATH = "tools[a].extensions[echo][0]: extension 'refusing' on tool 'echo' cannot bind"


def _attach_marked(document: dict[str, Any]) -> None:
    document["tools"] = copy.deepcopy(_MARKED_COMBO["tools"])


async def test_apply_env_change_refuses_a_marker_turning_a_setting_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest=copy.deepcopy(_MARKED_COMBO), env={"EXT_L": "ok"})
    service, admin, bus = _service(store, combo_check=ComboChecks(refuse="x"))

    with pytest.raises(ValueError, match=r"^tools\[a\]\.extensions\[echo\]\[0\]: extension 'refusing'"):
        await service.apply_env_change({"EXT_L": "x"})

    assert store.env == {"EXT_L": "ok"}
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_env_and_change_refuses_a_combo_the_check_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={}, env={})
    service, admin, bus = _service(store, combo_check=ComboChecks(refuse="x"))

    with pytest.raises(ValueError, match=r"^tools\[a\]\.extensions\[echo\]\[0\]: extension 'refusing'"):
        await service.apply_env_and_change(_prepare({"EXT_L": "x"}, _attach_marked))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_replace_env_refuses_a_combo_the_check_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest=copy.deepcopy(_MARKED_COMBO), env={"EXT_L": "ok"})
    service, admin, bus = _service(store, combo_check=ComboChecks(refuse="x"))
    snapshots: list[dict[str, str]] = []

    async def save_previous(stored: dict[str, str]) -> None:
        snapshots.append(stored)

    async def build_and_swap(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused profile builds nothing")

    with pytest.raises(ValueError, match=r"^tools\[a\]\.extensions\[echo\]\[0\]: extension 'refusing'"):
        await service.apply_replace_env({"EXT_L": "x"}, save_previous=save_previous, build_and_swap=build_and_swap)

    assert snapshots == []
    assert store.env == {"EXT_L": "ok"}
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.usefixtures(clean_server.__name__)
async def test_the_live_check_refuses_an_env_change_its_factory_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    async with booted_check_app():
        store = FakeConfigStore(manifest=copy.deepcopy(_MARKED_COMBO), env={"EXT_L": "ok"})
        service, admin, _bus = _service(store, combo_check=instance.app.extensions.check_combo)

        with pytest.raises(ValueError, match="bad label") as caught:
            await service.apply_env_change({"EXT_L": "x"})

        assert str(caught.value) == f"{_REFUSED_PATH}: bad label"
        assert store.env == {"EXT_L": "ok"}
        assert admin.calls == 0
