"""The env-writing doors restart the same workers for the same fleet reports: one decision serves them all.

``write_env`` (:meth:`~tai42_skeleton.config.service.ConfigService.apply_env_change`) and
``apply_profile`` (:meth:`~tai42_skeleton.config.service.ConfigService.apply_replace_env`) run
against one fixture set — a serve writer, a serve sibling and a backend sibling, each answering
the reload with the recycle-class keys it still holds off their bound value — and each case
expects the same roll and the same reply from both doors.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

# ``bus_settings`` is imported for its registration side effect: TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.config.recycle_policy import RecycleDecision
from tai42_skeleton.operations._broadcast import SELF_DEFERRED, apply_response, profile_apply_response

from .._fakes.bus import FakeBus
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

# The door's edge settles the writer's exit its change owes; these tests drive the door below it.
pytestmark = pytest.mark.usefixtures("writer_exit_edge")

_KEY = "TAI_BUS_HEARTBEAT_TTL"


@dataclass(frozen=True)
class _Case:
    """One fleet state: the stored env, the change, and which workers report the key after the reload."""

    stored: dict[str, str]
    change: dict[str, str]
    reporting: frozenset[str]


_CASES = {
    # A key only the backend sibling reports: the roll restarts it alone; the serve sibling is
    # skipped and the writer, clean, arms no exit.
    "only-the-backend-reports": _Case({_KEY: "5"}, {_KEY: "7"}, frozenset({"backend-1"})),
    # The writer's own reload reports the key: its restart follows as its exit after the reply.
    "the-writer-reports": _Case({_KEY: "5"}, {_KEY: "7"}, frozenset({"serve-writer"})),
    # A hot-only change while a sibling still reports a key from before: that sibling is restarted.
    "a-hot-change-with-an-earlier-drift": _Case({}, {"LOG_LEVEL": "debug"}, frozenset({"serve-b"})),
    # Every worker confirms a clean reload: no roll, an INFO line naming the clean workers.
    "all-clean": _Case({_KEY: "5"}, {_KEY: "7"}, frozenset()),
}


def _bus(reporting: frozenset[str]) -> FakeBus:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b", "backend-1"])
    backend = next(row for row in bus.rows if row.name == "backend-1")
    backend.kind = WorkerKind.backend
    for name in ("serve-b", "backend-1"):
        bus.remote_payloads[name] = {"status": "ok", "awaiting_respawn": [_KEY] if name in reporting else []}
    return bus


def _orchestrate() -> tuple[Callable[..., Awaitable[RecycleReport]], list[dict[str, Any]]]:
    """An orchestrator that recycles every expected worker the decision rolls over, with the keys it reported."""
    calls: list[dict[str, Any]] = []

    async def orchestrate(bus: Any, *, decision: RecycleDecision, expected: Any, **kwargs: Any) -> RecycleReport:
        calls.append({"decision": decision, "expected": dict(expected), **kwargs})
        rows = [
            RecycleRow(
                name=name,
                kind=kind.value,
                generation_before=generation,
                status=RECYCLED,
                keys=list(decision.reported[name].keys) if name in decision.reported else [],
            )
            for name, (kind, generation) in sorted(expected.items())
            if decision.rolls_over(name)
        ]
        return RecycleReport(rows=rows)

    return orchestrate, calls


async def _write_env(case: _Case, monkeypatch: pytest.MonkeyPatch, bus: FakeBus, orchestrate: Any) -> dict[str, Any]:
    writer_keys = [_KEY] if "serve-writer" in case.reporting else []
    admin = FakeReloadAdmin(result={"status": "ok", "env_keys": 1, "awaiting_respawn": writer_keys})
    service, _admin, _bus = _service(
        FakeConfigStore(env=dict(case.stored)), admin=admin, bus=bus, orchestrate=orchestrate
    )
    return apply_response(await service.apply_env_change(dict(case.change)))


async def _apply_profile(
    case: _Case, monkeypatch: pytest.MonkeyPatch, bus: FakeBus, orchestrate: Any
) -> dict[str, Any]:
    from tai42_skeleton.app.lifecycle import config_reload

    writer_keys = [_KEY] if "serve-writer" in case.reporting else []
    monkeypatch.setattr(config_reload, "awaiting_respawn", lambda: list(writer_keys))
    service, _admin, _bus = _service(FakeConfigStore(env=dict(case.stored)), bus=bus, orchestrate=orchestrate)

    async def save_previous(_env: dict[str, str]) -> None:
        return None

    async def build_and_swap(_env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        return Epoch(number=0)

    outcome = await service.apply_replace_env(
        dict(case.stored) | dict(case.change), save_previous=save_previous, build_and_swap=build_and_swap
    )
    return profile_apply_response(outcome)


_DOORS = {"write_env": _write_env, "apply_profile": _apply_profile}


@pytest.fixture(params=sorted(_DOORS))
def door(request: pytest.FixtureRequest) -> Any:
    return _DOORS[request.param]


async def _run(
    door: Any, case: _Case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _with_bus(monkeypatch)
    bus = _bus(case.reporting)
    # The reply's fan-out summary reads the process bus, so the doors run through this one.
    monkeypatch.setattr(instance.app, "_bus", bus)
    orchestrate, calls = _orchestrate()
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.config.recycle_roll"):
        reply = await door(case, monkeypatch, bus, orchestrate)
    return reply, calls


def _entries(reply: dict[str, Any]) -> list[dict[str, Any]]:
    roll = reply["recycle"]
    # The env-write reply nests the roll; the profile reply carries its lines at the top level.
    return roll["recycle"] if isinstance(roll, dict) else roll


def _skipped(reply: dict[str, Any]) -> list[str]:
    roll = reply["recycle"]
    return roll["skipped"] if isinstance(roll, dict) else reply["skipped"]


async def test_a_key_only_the_backend_reports_restarts_the_backend_alone(
    door: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reply, calls = await _run(door, _CASES["only-the-backend-reports"], monkeypatch, caplog)

    (call,) = calls
    assert call["decision"].clean == {"serve-b"}
    assert call["applier_self_deferred"] is False
    assert [(e["name"], e["status"], e["keys"]) for e in _entries(reply)] == [("backend-1", RECYCLED, [_KEY])]
    assert _skipped(reply) == ["serve-b"]
    assert reply["awaiting_respawn"] == []


async def test_a_writer_reporting_the_key_arms_its_own_exit(
    door: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reply, calls = await _run(door, _CASES["the-writer-reports"], monkeypatch, caplog)

    (call,) = calls
    assert call["decision"].clean == {"serve-b", "backend-1"}
    assert call["applier_self_deferred"] is True
    assert [(e["name"], e["status"], e["keys"]) for e in _entries(reply)] == [("serve-writer", SELF_DEFERRED, [_KEY])]
    assert sorted(_skipped(reply)) == ["backend-1", "serve-b"]
    assert reply["awaiting_respawn"] == []


async def test_a_hot_change_restarts_a_sibling_still_reporting_an_earlier_drift(
    door: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reply, calls = await _run(door, _CASES["a-hot-change-with-an-earlier-drift"], monkeypatch, caplog)

    (call,) = calls
    assert call["decision"].clean == {"backend-1"}
    assert [(e["name"], e["keys"]) for e in _entries(reply)] == [("serve-b", [_KEY])]
    assert "restart serve-b (reports TAI_BUS_HEARTBEAT_TTL)" in caplog.text


async def test_a_fleet_confirming_a_clean_reload_rolls_nothing(
    door: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reply, calls = await _run(door, _CASES["all-clean"], monkeypatch, caplog)

    assert calls == []
    assert _entries(reply) in (None, [])
    assert reply["awaiting_respawn"] == []
    assert (
        "is skipped: no worker needs a restart "
        "(confirmed clean: backend-1, serve-b; left running, its restart withheld: none; the writer "
        "'serve-writer' is clean)" in caplog.text
    )
