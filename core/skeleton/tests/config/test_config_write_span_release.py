"""A config store whose exclusive write span fails to release, after or instead of a persist.

Every write entry persists as the last statement inside the span, so an exit that raises after
the body completed is a failed release of a write that landed: the entry returns its result
with the store's error on ``span_release_error``, its reload and broadcast still run, and the
failure is logged at ERROR with its traceback. An exit that raises after the body raised leaves
the body's own error to propagate, and a cancellation the exit held is re-raised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest

# ``bus_settings`` is imported for its registration side effect — the apply's recycle
# classification reflects only IMPORTED settings classes, and TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.config.service import ApplyResult, OrphanEnvWriteError, ProfileApplyOutcome
from tai42_skeleton.operations._broadcast import FleetBroadcastError, apply_response, profile_apply_response

from .._fakes.bus import FakeBus
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    RecordingBus,
    _orchestrate_spy,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)
from .test_config_write_lock import _CountingSpanStore, _doors, _LoggingStore, _manifest, _World

_RELEASE_ERROR = "OSError: releasing the span failed"
_EXCLUSION_LOGGER = "tai42_skeleton.config.service.exclusion"


def _release_failure() -> OSError:
    return OSError("releasing the span failed")


def _release_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _EXCLUSION_LOGGER]


def _failing_world(monkeypatch: pytest.MonkeyPatch) -> _World:
    world = _World(monkeypatch)
    world.store = _LoggingStore(
        world.log, manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"}, fail_exit=_release_failure()
    )
    return world


_PERSISTS = {
    "apply_change": "mutate_manifest",
    "apply_replace": "replace_manifest",
    "apply_env_change": "write_env",
    "apply_env_and_change": "mutate_manifest",
    "apply_replace_env": "replace_env",
}


@pytest.mark.parametrize("entry", sorted(_PERSISTS))
async def test_a_write_whose_span_fails_to_release_returns_its_result_after_its_reload_and_broadcast(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, entry: str
) -> None:
    world = _failing_world(monkeypatch)
    admin = FakeReloadAdmin()
    bus = FakeBus()
    service = world.service(admin=admin, bus=bus)

    with caplog.at_level(logging.ERROR, logger=_EXCLUSION_LOGGER):
        result = await _doors(world, service)[entry]()

    assert _PERSISTS[entry] in world.log
    assert world.log[-1] == _PERSISTS[entry]
    assert [wire["op"] for wire, _targets, _local in bus.publish_calls] == ["reload_config"]
    if entry == "apply_replace_env":
        assert isinstance(result, ProfileApplyOutcome)
    else:
        assert isinstance(result, ApplyResult)
        assert admin.calls == 1
    assert result.span_release_error == _RELEASE_ERROR
    (record,) = _release_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is world.store.fail_exit
    assert "the write persisted and the change stands" in record.getMessage()


async def test_a_profile_apply_whose_span_fails_to_release_still_saves_previous_and_rolls_the_recycle(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"}, fail_exit=_release_failure())
    bus = FakeBus(origin="serve-applier", remotes=["serve-b"])
    orchestrate, seen = _orchestrate_spy()
    service, _admin, _bus = _service(store, bus=bus, orchestrate=orchestrate)
    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(dict(env))

    async def build_and_swap(_env: dict[str, str]) -> Epoch:
        return Epoch(number=0)

    with caplog.at_level(logging.ERROR, logger=_EXCLUSION_LOGGER):
        outcome = await service.apply_replace_env(
            {"TAI_BUS_NAMESPACE": "new"}, save_previous=save_previous, build_and_swap=build_and_swap
        )

    assert saved == [{"TAI_BUS_NAMESPACE": "old"}]
    assert store.env == {"TAI_BUS_NAMESPACE": "new"}
    assert [wire["op"] for wire, _targets, _local in bus.publish_calls] == ["reload_config"]
    assert seen["excluded_name"] == "serve-applier"
    assert outcome.recycle is not None
    assert [row.name for row in outcome.recycle.rows] == ["serve-b"]
    assert outcome.span_release_error == _RELEASE_ERROR
    assert len(_release_records(caplog)) == 1


async def test_a_refused_write_whose_span_fails_to_release_raises_its_own_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = _failing_world(monkeypatch)
    admin = FakeReloadAdmin()

    def dangling(document: dict[str, Any]) -> None:
        document["mcp"][0]["config"]["headers"] = {"X": "!ENV ${NOPE_MISSING}"}

    with caplog.at_level(logging.ERROR, logger=_EXCLUSION_LOGGER), pytest.raises(ValueError, match="dangling"):
        await world.service(admin=admin).apply_change(dangling)

    assert world.log == []
    assert world.store.persisted == []
    assert admin.calls == 0
    (record,) = _release_records(caplog)
    assert record.exc_info is not None
    assert record.exc_info[1] is world.store.fail_exit
    assert "the write raised before it completed, and its own error is raised" in record.getMessage()


class _OrphaningStore(FakeConfigStore):
    """A store whose manifest persist fails, so a combined env+manifest write leaves its env write landed."""

    def mutate_manifest(self, mutator: Any) -> dict[str, Any]:
        raise RuntimeError("manifest persist boom")


async def test_an_orphan_env_write_whose_span_fails_to_release_raises_the_orphan_and_logs_that_the_write_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    store = _OrphaningStore(manifest={"mcp": []}, fail_exit=_release_failure())
    service, admin, _bus = _service(store)

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Any]:
        return {"GH": "the-secret"}, _add_server

    with caplog.at_level(logging.ERROR, logger=_EXCLUSION_LOGGER), pytest.raises(OrphanEnvWriteError):
        await service.apply_env_and_change(prepare)

    assert store.env_writes == [{"GH": "the-secret"}]
    assert admin.calls == 0
    (record,) = _release_records(caplog)
    assert record.exc_info is not None
    assert record.exc_info[1] is store.fail_exit
    assert "the write raised before it completed, and its own error is raised" in record.getMessage()


def _add_server(document: dict[str, Any]) -> None:
    document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]


async def test_a_failed_broadcast_after_a_failed_release_names_both_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, fail_exit=_release_failure())
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    with pytest.raises(FleetBroadcastError) as raised:
        await service.apply_change(_add_server)

    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    error = raised.value.report.error
    assert error is not None
    assert "WRONGTYPE" in error
    assert f"the store's write span also failed to release: {_RELEASE_ERROR}" in error
    assert raised.value.span_release_error == _RELEASE_ERROR


async def test_a_failed_local_reload_after_a_failed_release_raises_the_reload_error_and_notes_the_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, fail_exit=_release_failure())
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    service, _admin, bus = _service(store, admin=admin)

    with pytest.raises(FleetBroadcastError) as raised:
        await service.apply_change(_add_server)

    assert len(bus.publish_calls) == 1
    assert "reload boom" in str(raised.value)
    assert raised.value.report.error == f"the store's write span also failed to release: {_RELEASE_ERROR}"
    assert raised.value.span_release_error == _RELEASE_ERROR


async def test_a_failed_profile_broadcast_after_a_failed_release_carries_the_release_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _failing_world(monkeypatch)
    bus = cast("Any", RecordingBus(publish_error=RuntimeError("WRONGTYPE")))
    service = world.service(admin=FakeReloadAdmin(), bus=bus)

    with pytest.raises(FleetBroadcastError) as raised:
        await _doors(world, service)["apply_replace_env"]()

    assert raised.value.span_release_error == _RELEASE_ERROR
    assert raised.value.report.error == (
        f"RuntimeError: WRONGTYPE (the store's write span also failed to release: {_RELEASE_ERROR})"
    )


async def test_a_writer_cancelled_while_its_span_fails_to_release_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = _World(monkeypatch)
    store = _CountingSpanStore(
        world.log, manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"}, fail_exit=_release_failure()
    )
    world.store = store
    admin = FakeReloadAdmin()
    bus = FakeBus()
    store.hold_next_exit = True

    with caplog.at_level(logging.ERROR, logger=_EXCLUSION_LOGGER):
        writer = asyncio.create_task(world.service(admin=admin, bus=bus).apply_env_change({"OTHER": "v"}))
        assert await asyncio.to_thread(store.exiting.wait, 10), "the span's exit never started"
        writer.cancel()
        for _ in range(50):
            await asyncio.sleep(0)
        assert not writer.done(), "the writer returned while its span's exit was still running"
        store.release_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await writer

    assert world.log == ["write_env"]
    assert store.env_writes == [{"OTHER": "v"}]
    assert admin.calls == 0
    assert bus.publish_calls == []
    (record,) = _release_records(caplog)
    assert record.exc_info is not None
    assert record.exc_info[1] is store.fail_exit
    assert "the writer was cancelled" in record.getMessage()


def test_the_apply_response_carries_the_span_release_error() -> None:
    fleet = FleetResult(op="reload_config", results=[])
    local = {"status": "ok", "env_keys": 1}
    held = ApplyResult(fleet=fleet, local=local, span_release_error=_RELEASE_ERROR)
    assert apply_response(held)["span_release_error"] == _RELEASE_ERROR
    assert apply_response(ApplyResult(fleet=fleet, local=local))["span_release_error"] is None


def test_the_profile_apply_response_carries_the_span_release_error() -> None:
    outcome = ProfileApplyOutcome(
        hot=["A"],
        recycle=None,
        self_identity=WorkerIdentity(name="serve-applier", kind=WorkerKind.serve, pid=1, generation=1),
        serve_affecting=False,
        fleet=FleetResult(op="reload_config", results=[]),
        span_release_error=_RELEASE_ERROR,
    )
    assert profile_apply_response(outcome)["span_release_error"] == _RELEASE_ERROR


@pytest.mark.parametrize(
    ("prior_fails", "continued_fails", "expected"),
    [
        (True, False, _RELEASE_ERROR),
        (False, True, "OSError: the continued release failed"),
        (True, True, f"{_RELEASE_ERROR}; OSError: the continued release failed"),
    ],
)
async def test_a_reload_that_continues_a_change_keeps_each_run_s_failed_release(
    monkeypatch: pytest.MonkeyPatch, prior_fails: bool, continued_fails: bool, expected: str
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, fail_exit=_release_failure() if prior_fails else None)
    service, _admin, _bus = _service(store)
    prior = await service.apply_change(_add_server)
    store.fail_exit = OSError("the continued release failed") if continued_fails else None

    result = await service.reapply_manifest(continues=prior)

    assert result.span_release_error == expected
