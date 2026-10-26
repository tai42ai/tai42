"""Deployment ownership across the fleet: a key any worker's deployment sets is the deployment's everywhere.

Each worker publishes the names its deployment sets on its presence row; every judge of ownership
reads the union over the census, so a key only a sibling process was started with is refused,
ignored by the reload apply and left out of the env diff on this process too.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.errors import ClientDisconnectedError
from tai42_kit.settings import TaiBaseSettings

from tai42_skeleton.app import applied_env
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, WorkerKind, WorkerRow, WorkerState
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.config.boundary import counting_diff_keys, refuse_deployment_owned
from tai42_skeleton.config.deployment_env import (
    DeploymentOwnership,
    fleet_deployment_ownership,
    known_fleet_env,
    published_deployment_keys,
    published_env_readers,
    reset_deployment_env,
    resolve_deployment_ownership,
)

from .._fakes.bus import FakeBus
from .._helpers import recapture_deployment_env
from .fake_pipeline import (
    FakeConfigStore,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

SIBLING_KEY = "FLEET_OWNERSHIP_TEST_SIBLING_ONLY"


def _sibling_bus(keys: list[str], *, state: WorkerState = WorkerState.ready, pttl_ms: int | None = None) -> FakeBus:
    """A bus whose one sibling row publishes ``keys`` as the names its deployment sets."""
    bus = FakeBus(origin="serve-writer", remotes=["backend-1"])
    sibling = bus.rows[1]
    sibling.deployment_keys = list(keys)
    sibling.state = state
    if pttl_ms is not None:
        sibling.pttl_ms = pttl_ms
    return bus


class _OutageBus(FakeBus):
    """A bus that answers until ``down`` is set, then fails every read the way a severed bus does.

    ``publish`` folds the outage into the bus-unreachable report, as the real bus does.
    """

    down = False

    async def census(self) -> list[WorkerRow]:
        if self.down:
            raise ClientDisconnectedError("bus down")
        return await super().census()

    async def expected_at_start(self) -> dict[str, int]:
        if self.down:
            raise ClientDisconnectedError("bus down")
        return await super().expected_at_start()

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        if self.down:
            return FleetResult(op=op.op, reachable=False, error="ClientDisconnectedError: bus down")
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


def _outage_bus(keys: list[str]) -> _OutageBus:
    """An outage bus whose one sibling row publishes ``keys`` as the names its deployment sets."""
    bus = _OutageBus(origin="serve-writer", remotes=["backend-1"])
    bus.rows[1].deployment_keys = list(keys)
    return bus


@pytest.fixture
def restore_applied_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep the epoch's applied-key set and the test keys out of the next test."""
    loaded_before = set(applied_env._loaded_env_keys)
    monkeypatch.delenv(SIBLING_KEY, raising=False)
    monkeypatch.delenv("FLEET_OWNERSHIP_TEST_OTHER", raising=False)
    yield
    os.environ.pop(SIBLING_KEY, None)
    os.environ.pop("FLEET_OWNERSHIP_TEST_OTHER", None)
    applied_env._loaded_env_keys = loaded_before


async def test_a_key_only_a_sibling_sets_is_owned_on_this_process() -> None:
    ownership = await fleet_deployment_ownership(_sibling_bus([SIBLING_KEY]))

    assert not DeploymentOwnership().owns(SIBLING_KEY)
    assert ownership.owns(SIBLING_KEY)
    with pytest.raises(ValueError, match=rf"set by the deployment: {SIBLING_KEY}"):
        refuse_deployment_owned({SIBLING_KEY: "from-editor"}, {}, ownership)


async def test_every_census_row_counts_whatever_its_state_or_freshness() -> None:
    # A worker that is leaving, returning or late on its beat still owns what it was started with.
    resyncing = await fleet_deployment_ownership(_sibling_bus([SIBLING_KEY], state=WorkerState.resyncing))
    recycling = await fleet_deployment_ownership(_sibling_bus([SIBLING_KEY], state=WorkerState.recycling))
    stale = await fleet_deployment_ownership(_sibling_bus([SIBLING_KEY], pttl_ms=1))

    assert resyncing.owns(SIBLING_KEY)
    assert recycling.owns(SIBLING_KEY)
    assert stale.owns(SIBLING_KEY)


async def test_the_learning_read_raises_when_the_bus_cannot_answer() -> None:
    bus = _outage_bus([])
    bus.down = True

    with pytest.raises(ClientDisconnectedError, match="bus down"):
        await fleet_deployment_ownership(bus)


async def test_a_use_door_judges_by_the_names_last_read_when_the_bus_cannot_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = _outage_bus([SIBLING_KEY])
    await fleet_deployment_ownership(bus)
    bus.down = True

    with caplog.at_level(logging.WARNING):
        ownership = await resolve_deployment_ownership(bus, "read_env")

    assert ownership.owns(SIBLING_KEY)
    assert SIBLING_KEY in known_fleet_env().keys
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "'read_env'" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


async def test_before_any_read_the_fallback_is_this_process_alone(caplog: pytest.LogCaptureFixture) -> None:
    reset_deployment_env()
    recapture_deployment_env()
    bus = _outage_bus([SIBLING_KEY])
    bus.down = True

    with caplog.at_level(logging.WARNING):
        ownership = await resolve_deployment_ownership(bus, "x")

    assert not ownership.owns(SIBLING_KEY)
    assert known_fleet_env().keys == frozenset()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_a_non_transport_census_error_still_raises() -> None:
    class _BrokenBus(FakeBus):
        async def census(self) -> list[WorkerRow]:
            raise ValueError("bad row")

    with pytest.raises(ValueError, match="bad row"):
        await resolve_deployment_ownership(_BrokenBus(), "read_env")


@pytest.mark.usefixtures("restore_applied_env")
async def test_a_successful_read_refreshes_the_names_a_late_settings_group_withdraws_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read on any door records the fleet's names; a settings group registered afterwards
    # judges the applied keys by them without reading the census itself.
    monkeypatch.setenv(SIBLING_KEY, "stored-copy")
    applied_env._loaded_env_keys = {*applied_env._loaded_env_keys, SIBLING_KEY}

    await resolve_deployment_ownership(_sibling_bus([SIBLING_KEY]), "read_env")

    class _FleetRefreshGroup(TaiBaseSettings):
        """A group registered after the read."""

        model_config = SettingsConfigDict(env_prefix="FLEET_OWNERSHIP_TEST_REFRESH_")

        value: str | None = None

    assert SIBLING_KEY not in os.environ
    assert SIBLING_KEY not in applied_env.applied_env_keys()
    assert _FleetRefreshGroup.__name__ == "_FleetRefreshGroup"


@pytest.mark.usefixtures("restore_applied_env")
async def test_a_discarded_build_restore_withdraws_by_the_names_last_read(monkeypatch: pytest.MonkeyPatch) -> None:
    await resolve_deployment_ownership(_sibling_bus([SIBLING_KEY]), "diff_profile")

    applied_env.restore_env({**os.environ, SIBLING_KEY: "stored-copy"}, {SIBLING_KEY})

    assert SIBLING_KEY not in os.environ
    assert SIBLING_KEY not in applied_env.applied_env_keys()


async def test_the_own_row_carries_this_process_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIBLING_KEY, "from-this-deployment")
    recapture_deployment_env()

    rows = await FakeBus(origin="serve-writer").census()

    assert SIBLING_KEY in rows[0].deployment_keys
    assert rows[0].deployment_keys == list(published_deployment_keys())


async def test_an_env_write_of_a_key_only_a_sibling_sets_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, _bus = _service(store, bus=_sibling_bus([SIBLING_KEY]))

    with pytest.raises(ValueError, match=rf"set by the deployment: {SIBLING_KEY}"):
        await service.apply_env_change({SIBLING_KEY: "from-editor"})

    assert store.env_writes == []
    assert admin.calls == 0


async def test_a_profile_setting_a_key_only_a_sibling_sets_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, _bus = _service(store, bus=_sibling_bus([SIBLING_KEY]))

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(ValueError, match=rf"set by the deployment: {SIBLING_KEY}"):
        await service.apply_replace_env({SIBLING_KEY: "from-profile"}, save_previous=save_previous)

    assert saved == []
    assert admin.calls == 0


@pytest.mark.usefixtures("restore_applied_env")
def test_the_reload_apply_skips_a_key_a_sibling_sets_and_names_it(caplog: pytest.LogCaptureFixture) -> None:
    ownership = DeploymentOwnership([SIBLING_KEY])

    with caplog.at_level(logging.WARNING):
        applied_env._apply_env({SIBLING_KEY: "stored-copy", "FLEET_OWNERSHIP_TEST_OTHER": "v"}, ownership)

    assert SIBLING_KEY not in os.environ
    assert os.environ["FLEET_OWNERSHIP_TEST_OTHER"] == "v"
    assert SIBLING_KEY not in applied_env.applied_env_keys()
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and SIBLING_KEY in r.getMessage()]
    assert len(lines) == 1
    assert "stored-copy" not in lines[0]


def test_the_env_diff_leaves_out_a_key_a_sibling_sets() -> None:
    ownership = DeploymentOwnership([SIBLING_KEY])

    assert ownership.counting({SIBLING_KEY: "a", "FLEET_OWNERSHIP_TEST_OTHER": "b"}) == {
        "FLEET_OWNERSHIP_TEST_OTHER": "b"
    }
    assert counting_diff_keys({SIBLING_KEY: "a"}, {SIBLING_KEY: "b"}, ownership) == set()
    assert counting_diff_keys({SIBLING_KEY: "a"}, {}, ownership) == set()


def test_a_sibling_name_owns_every_accepted_name_of_its_field() -> None:
    class _FleetAliasedGroup(TaiBaseSettings):
        """One field read under two env names."""

        model_config = SettingsConfigDict(env_prefix="FLEET_OWNERSHIP_TEST_ALIAS_")

        endpoint: str | None = Field(
            default=None,
            validation_alias=AliasChoices("FLEET_OWNERSHIP_TEST_ALIAS_ENDPOINT", "FLEET_OWNERSHIP_TEST_ALIAS_URL"),
        )

    ownership = DeploymentOwnership(["FLEET_OWNERSHIP_TEST_ALIAS_URL"])

    assert ownership.owns("FLEET_OWNERSHIP_TEST_ALIAS_ENDPOINT")
    assert _FleetAliasedGroup.__name__ == "_FleetAliasedGroup"


def test_a_settings_group_registered_later_is_judged_through_the_published_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # The readers a worker publishes follow its registry: a group registered after the capture
    # makes the field's other accepted names the deployment's in the fleet view of its row.
    monkeypatch.setenv("FLEET_OWNERSHIP_TEST_LATE_URL", "https://deployment.invalid")
    recapture_deployment_env()

    class _FleetLateGroup(TaiBaseSettings):
        """One field read under two env names, registered after the capture."""

        model_config = SettingsConfigDict(env_prefix="FLEET_OWNERSHIP_TEST_LATE_")

        endpoint: str | None = Field(
            default=None,
            validation_alias=AliasChoices("FLEET_OWNERSHIP_TEST_LATE_ENDPOINT", "FLEET_OWNERSHIP_TEST_LATE_URL"),
        )

    readers = published_env_readers()
    fleet = DeploymentOwnership(published_deployment_keys(), {readers.digest: readers.readers})

    assert "FLEET_OWNERSHIP_TEST_LATE_ENDPOINT" not in published_deployment_keys()
    assert fleet.owns("FLEET_OWNERSHIP_TEST_LATE_ENDPOINT")
    assert list(published_deployment_keys()) == sorted(published_deployment_keys())
    assert _FleetLateGroup.__name__ == "_FleetLateGroup"


# -- a save while the bus cannot answer ---------------------------------------


async def test_an_env_write_during_an_outage_persists_reloads_and_reports_the_bus_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    bus = _outage_bus([SIBLING_KEY])
    service, admin, _bus = _service(store, bus=bus)
    await fleet_deployment_ownership(bus)
    bus.down = True

    with caplog.at_level(logging.WARNING):
        result = await service.apply_env_change({"FLEET_OWNERSHIP_TEST_OTHER": "v"})

    assert store.env_writes == [{"FLEET_OWNERSHIP_TEST_OTHER": "v"}]
    assert admin.calls == 1
    assert result.fleet.reachable is False
    assert result.fleet.results == []
    assert any("'apply_env_change'" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_an_env_write_during_an_outage_still_refuses_a_key_a_sibling_was_read_to_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    bus = _outage_bus([SIBLING_KEY])
    service, admin, _bus = _service(store, bus=bus)
    await fleet_deployment_ownership(bus)
    bus.down = True

    with pytest.raises(ValueError, match=rf"set by the deployment: {SIBLING_KEY}"):
        await service.apply_env_change({SIBLING_KEY: "from-editor"})

    assert store.env_writes == []
    assert admin.calls == 0


async def test_a_combined_env_and_manifest_write_during_an_outage_persists_and_reloads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    bus = _outage_bus([SIBLING_KEY])
    service, admin, _bus = _service(store, bus=bus)
    await fleet_deployment_ownership(bus)
    bus.down = True

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Any]:
        def mutator(document: dict[str, Any]) -> None:
            document["mcp"] = []

        return {"FLEET_OWNERSHIP_TEST_OTHER": "v"}, mutator

    with caplog.at_level(logging.WARNING):
        result = await service.apply_env_and_change(prepare)

    assert store.env_writes == [{"FLEET_OWNERSHIP_TEST_OTHER": "v"}]
    assert admin.calls == 1
    assert result.fleet.reachable is False
    assert any("'apply_env_and_change'" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_a_recycle_class_write_during_an_outage_reports_the_stopped_roll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import tai42_skeleton.backend.settings  # noqa: F401  — registers the recycle-class BACKEND_MANIFEST_KEY

    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"BACKEND_MANIFEST_KEY": "OLD"})
    bus = _outage_bus([])
    service, admin, _bus = _service(store, bus=bus)
    bus.down = True

    with caplog.at_level(logging.WARNING):
        result = await service.apply_env_change({"BACKEND_MANIFEST_KEY": "NEW"})

    assert store.env == {"BACKEND_MANIFEST_KEY": "NEW"}
    assert admin.calls == 1
    assert result.fleet.reachable is False
    assert result.recycle is not None
    assert result.recycle.stopped is not None
    assert result.recycle.stopped.kind == WorkerKind.backend.value
    assert "bus unreachable" in result.recycle.stopped.detail
    assert result.self_exit_armed is False
    assert any(
        "recycle targets for 'this env change'" in r.getMessage() and r.exc_info is not None
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
