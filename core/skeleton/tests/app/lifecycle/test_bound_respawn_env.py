"""The baseline a reload judges a recycle-class key against: the value its process bound it under.

The boot env bridge binds every registered respawn-class setting to the env it just applied; a
group first registered later (a plugin loaded by an install or a manifest change) binds its own
values at its registration, so a resource built under the value already in place awaits nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import ClassVar

import pytest
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import ReloadClass, TaiBaseSettings, bind_respawn_class_env, reset_all_settings
from tai42_kit.settings import registry as settings_registry

# Imported for its registration side effect: the bus group is recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle.config_reload import awaiting_respawn

_BUS_KEY = "TAI_BUS_HEARTBEAT_TTL"
_LATE_KEY = "LATEPLUGIN_POOL_URL"


@pytest.fixture(autouse=True)
def _restore_process_env_and_registry() -> Iterator[None]:
    """Restore ``os.environ``, the epoch's loaded-key tracker and the settings registry around each test."""
    env = dict(os.environ)
    loaded = set(epoch_mod._loaded_env_keys)
    registered = dict(settings_registry._REGISTRY)
    yield
    os.environ.clear()
    os.environ.update(env)
    epoch_mod._loaded_env_keys = loaded
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


def test_the_boot_bridge_binds_the_env_it_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_BUS_KEY, "15")
    epoch_mod._loaded_env_keys = set()
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {_BUS_KEY: "7"})

    app._apply_stored_env_at_boot()

    assert awaiting_respawn() == []
    epoch_mod.apply_env_and_reset_settings({_BUS_KEY: "9"})
    assert awaiting_respawn() == [_BUS_KEY]


def test_a_key_first_registered_after_its_value_landed_awaits_no_respawn() -> None:
    os.environ.pop(_LATE_KEY, None)
    bind_respawn_class_env()

    # The install-time value lands in the process env (the reload's env apply).
    os.environ[_LATE_KEY] = "postgres://pool"
    # The reload imports the plugin: its group registers the key recycle-class and its pooled
    # resource is built under the value just landed.
    assert _define_late_plugin_group()().pool_url == "postgres://pool"

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
