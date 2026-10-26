"""The baseline a reload judges a recycle-class key against: the value its process bound it under.

The boot env bridge binds every registered respawn-class setting to the env it just applied; a
group first registered later (a plugin loaded by an install or a manifest change) binds its own
values at its registration, so a resource built under the value already in place awaits nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from pydantic_settings import SettingsConfigDict
from tai42_contract.errors import ClientDisconnectedError
from tai42_kit.settings import ReloadClass, TaiBaseSettings, bind_respawn_class_env, reset_all_settings
from tai42_kit.settings import registry as settings_registry

# Imported for its registration side effect: the bus group is recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import applied_env
from tai42_skeleton.app.boot_rules import ConfigInvariantError
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.bus_settings import bus_settings
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle.config_reload import awaiting_respawn
from tai42_skeleton.config.deployment_env import DeploymentOwnership, reset_deployment_env
from tai42_skeleton.manifest import Manifest

from ..._fakes.bus import FakeBus
from ..._helpers import recapture_deployment_env

_BUS_KEY = "TAI_BUS_HEARTBEAT_TTL"
_BUS_URL = "redis://bus.test:6379/0"
_LATE_KEY = "LATEPLUGIN_POOL_URL"


@pytest.fixture(autouse=True)
def _restore_process_env_and_registry() -> Iterator[None]:
    """Restore ``os.environ``, the epoch's loaded-key tracker and the settings registry around each test."""
    env = dict(os.environ)
    loaded = set(applied_env._loaded_env_keys)
    registered = dict(settings_registry._REGISTRY)
    yield
    os.environ.clear()
    os.environ.update(env)
    applied_env._loaded_env_keys = loaded
    settings_registry._REGISTRY.clear()
    settings_registry._REGISTRY.update(registered)
    reset_all_settings()


def _define_late_plugin_group() -> type[TaiBaseSettings]:
    """Define (or re-define, as a reload's re-import does) a plugin's recycle-class settings group."""

    class _LatePluginSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="LATEPLUGIN_")
        reload_class: ClassVar[ReloadClass] = "recycle"

        pool_url: str = ""

    return _LatePluginSettings


@pytest.fixture
def boot_bus(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Hand the boot env bridge the bus a test puts in the list; the app's bus is restored afterwards."""
    buses: list[Any] = [FakeBus(origin="serve-1")]
    monkeypatch.setattr(app, "_bus", None)
    monkeypatch.setattr(app, "_build_bus", lambda kind: buses[0])
    return buses


def _sibling_publishing(keys: list[str]) -> FakeBus:
    """A bus whose one sibling row publishes ``keys`` as the names its deployment sets."""
    bus = FakeBus(origin="serve-1", remotes=["backend-1"])
    bus.rows[1].deployment_keys = list(keys)
    return bus


class _DownCensusBus(FakeBus):
    """A bus whose census fails the way a severed bus does."""

    async def census(self) -> list[Any]:
        raise ClientDisconnectedError("bus down")


class _BusesByTheirSettings:
    """Build each bus the boot env bridge asks for under the bus settings in force at that build.

    Settings that configure no bus give a busless bus, whose census is this process's own row;
    settings that configure one give a bus whose one sibling publishes ``sibling_keys``.
    """

    def __init__(self) -> None:
        self.sibling_keys: list[str] = []
        self.built: list[FakeBus] = []

    def __call__(self, kind: WorkerKind) -> FakeBus:
        settings = bus_settings()
        bus = _sibling_publishing(self.sibling_keys) if settings.enabled else FakeBus(origin="serve-1")
        bus.settings = settings
        bus.ttl = settings.heartbeat_ttl
        self.built.append(bus)
        return bus


@pytest.fixture
def buses_by_their_settings(monkeypatch: pytest.MonkeyPatch) -> _BusesByTheirSettings:
    """Hand the boot env bridge a bus built under the settings in force; the app's bus is restored afterwards."""
    buses = _BusesByTheirSettings()
    monkeypatch.setattr(app, "_bus", None)
    monkeypatch.setattr(app, "_build_bus", buses)
    return buses


def _no_bus_in_the_deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL", "TAI_BUS_NAMESPACE", _BUS_KEY):
        monkeypatch.delenv(name, raising=False)
    recapture_deployment_env()
    reset_all_settings()
    applied_env._loaded_env_keys = set()


@pytest.mark.usefixtures("boot_bus")
async def test_the_boot_bridge_binds_the_env_it_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_BUS_KEY, "15")
    applied_env._loaded_env_keys = set()
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {_BUS_KEY: "7"})

    await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    assert awaiting_respawn() == []
    applied_env.apply_env_and_reset_settings({_BUS_KEY: "9"}, DeploymentOwnership())
    assert awaiting_respawn() == [_BUS_KEY]


def test_a_key_first_registered_after_its_value_landed_awaits_no_respawn() -> None:
    os.environ.pop(_LATE_KEY, None)
    bind_respawn_class_env()

    # The install-time value lands in the process env (the reload's env apply).
    os.environ[_LATE_KEY] = "postgres://pool"
    # The reload imports the plugin: its group registers the key recycle-class and its pooled
    # resource is built under the value just landed.
    assert _define_late_plugin_group()().model_dump() == {"pool_url": "postgres://pool"}

    assert awaiting_respawn() == []


def test_a_late_registered_key_awaits_a_respawn_once_its_value_moves_and_a_re_import_keeps_that() -> None:
    os.environ.pop(_LATE_KEY, None)
    bind_respawn_class_env()
    os.environ[_LATE_KEY] = "postgres://pool"
    _define_late_plugin_group()

    os.environ[_LATE_KEY] = "postgres://other"
    assert awaiting_respawn() == [_LATE_KEY]

    _define_late_plugin_group()
    assert awaiting_respawn() == [_LATE_KEY]


async def test_the_boot_bridge_never_applies_a_stored_copy_of_a_key_another_workers_deployment_sets(
    monkeypatch: pytest.MonkeyPatch, boot_bus: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(_BUS_KEY, raising=False)
    applied_env._loaded_env_keys = set()
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {_BUS_KEY: "4321"})
    boot_bus[0] = _sibling_publishing([_BUS_KEY])

    with caplog.at_level(logging.WARNING):
        await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    assert _BUS_KEY not in os.environ
    assert _BUS_KEY not in applied_env.applied_env_keys()
    # Nothing was bound on the stored copy, so no respawn is owed for it.
    assert awaiting_respawn() == []
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and _BUS_KEY in r.getMessage()]
    assert len(lines) == 1
    assert "4321" not in lines[0]
    assert app._bus is boot_bus[0]


async def test_a_boot_bridge_that_cannot_read_the_census_judges_by_its_own_env_and_says_so(
    monkeypatch: pytest.MonkeyPatch, boot_bus: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(_BUS_KEY, raising=False)
    applied_env._loaded_env_keys = set()
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {_BUS_KEY: "4321"})
    boot_bus[0] = _DownCensusBus(origin="serve-1", remotes=["backend-1"])

    with caplog.at_level(logging.WARNING):
        await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    assert os.environ[_BUS_KEY] == "4321"
    assert _BUS_KEY in applied_env.applied_env_keys()
    assert any(r.levelno == logging.WARNING and "boot env bridge" in r.getMessage() for r in caplog.records)


async def test_the_bus_joins_under_the_bus_settings_the_boot_bridge_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deployment env configures no bus; the stored env does, with its own heartbeat.
    for name in ("TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL", _BUS_KEY):
        monkeypatch.delenv(name, raising=False)
    reset_deployment_env()
    reset_all_settings()
    applied_env._loaded_env_keys = set()
    monkeypatch.setattr(app, "_bus", None)
    monkeypatch.setattr(
        app.config.config_manager,
        "read_env",
        lambda: {"TAI_BUS_REDIS_URL": "redis://127.0.0.1:1/0", _BUS_KEY: "21"},
    )

    await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    # The census before the apply was this busless process's own row; the bus it joins is the
    # one the applied env configures.
    bus = app._bus
    assert bus is not None
    assert bus._local is False
    assert bus.heartbeat_ttl == 21.0


async def test_a_process_whose_bus_is_stored_withdraws_a_copy_another_workers_deployment_sets_before_binding_it(
    monkeypatch: pytest.MonkeyPatch, buses_by_their_settings: _BusesByTheirSettings, caplog: pytest.LogCaptureFixture
) -> None:
    _no_bus_in_the_deployment_env(monkeypatch)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"TAI_BUS_REDIS_URL": _BUS_URL, _BUS_KEY: "21"})
    buses_by_their_settings.sibling_keys = [_BUS_KEY]

    with caplog.at_level(logging.WARNING):
        await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    assert _BUS_KEY not in os.environ
    assert _BUS_KEY not in applied_env.applied_env_keys()
    assert os.environ["TAI_BUS_REDIS_URL"] == _BUS_URL
    # The census was read on the bus the stored env configures, and the bus it joins is built
    # under the withdrawn heartbeat.
    assert buses_by_their_settings.built[0].settings.redis.redis_url == _BUS_URL
    bus = app._bus
    assert bus is not None
    assert bus is buses_by_their_settings.built[-1]
    assert bus.settings.enabled
    assert bus.heartbeat_ttl == 15.0
    # Nothing was bound on the stored copy, so no respawn is owed for it.
    assert awaiting_respawn() == []
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and _BUS_KEY in r.getMessage()]
    assert len(lines) == 1
    assert "21" not in lines[0]


async def test_a_process_reaching_its_bus_only_through_a_stored_copy_another_workers_deployment_sets_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch, buses_by_their_settings: _BusesByTheirSettings
) -> None:
    _no_bus_in_the_deployment_env(monkeypatch)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"TAI_BUS_REDIS_URL": _BUS_URL, _BUS_KEY: "21"})
    buses_by_their_settings.sibling_keys = ["TAI_BUS_REDIS_URL", _BUS_KEY]

    with pytest.raises(ConfigInvariantError, match="TAI_BUS_REDIS_URL") as refused:
        await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    assert _BUS_URL not in str(refused.value)
    assert "TAI_BUS_REDIS_URL" not in os.environ


async def test_a_process_whose_stored_namespace_another_workers_deployment_sets_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch, buses_by_their_settings: _BusesByTheirSettings
) -> None:
    _no_bus_in_the_deployment_env(monkeypatch)
    monkeypatch.setenv("TAI_BUS_REDIS_URL", _BUS_URL)
    recapture_deployment_env()
    reset_all_settings()
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"TAI_BUS_NAMESPACE": "ns-two"})
    buses_by_their_settings.sibling_keys = ["TAI_BUS_NAMESPACE"]

    with pytest.raises(ConfigInvariantError, match="TAI_BUS_NAMESPACE") as refused:
        await app._apply_stored_env_at_boot(kind=WorkerKind.serve)

    assert "ns-two" not in str(refused.value)
    assert "TAI_BUS_NAMESPACE" not in os.environ


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_app_context_refuses_to_start_without_the_bus_the_boot_bridge_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "_bus", None)

    with pytest.raises(RuntimeError, match="read_boot_manifest builds the worker bus"):
        async with app.app_context(Manifest.model_validate({})):
            pass
