"""The audit of owned settings prefixes over the env a boot or reload applies and the boot inputs.

The applied env is the stored env read through the config manager the app holds (or the env a
reload proposes), so a synthetic non-file manager stand-in (no file on disk) proves a provider's
own store is audited at boot and on reload.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import pytest
from tai42_contract.config.manager import ConfigManager
from tai42_kit.settings import UnknownOwnedSettingError, reset_all_settings

from tai42_skeleton.settings.owned_settings import (
    OWNED_SETTINGS_MODULES,
    refuse_unknown_owned_applied_env,
    refuse_unknown_owned_env_write,
    require_known_owned_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


class _StandInManager(ConfigManager):
    """A non-file config manager: ``read_env`` answers from memory; the manifest is empty."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def read_env(self) -> dict[str, str]:
        return dict(self._env)

    def write_env(self, config: dict[str, str]) -> None:
        raise NotImplementedError

    def replace_env(self, config: dict[str, str]) -> None:
        raise NotImplementedError

    def read_manifest(self) -> dict[str, Any]:
        return {}

    def read_manifest_preserved(self) -> dict[str, Any]:
        raise NotImplementedError

    def read_defaults_manifest(self) -> dict[str, Any]:
        raise NotImplementedError

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        raise NotImplementedError

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _clean_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in an empty directory so no ambient ``.env`` is audited."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def _restore_applied_env() -> Iterator[None]:
    """Restore ``os.environ`` and the applied-env tracker after a test that boots or reloads."""
    from tai42_skeleton.app import epoch as epoch_mod

    snapshot = dict(os.environ)
    loaded_before = set(epoch_mod._loaded_env_keys)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = loaded_before
    reset_all_settings()


_LINK_FAMILY = {
    "ACCESS_CONTROL_SERVICE_HOST": "10.0.0.2",
    "ACCESS_CONTROL_PORT": "tcp://10.0.0.2:8000",
    "ACCESS_CONTROL_PORT_8000_TCP": "tcp://10.0.0.2:8000",
}


def _write_dotenv(tmp_path: Path, env: dict[str, str]) -> None:
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))


def test_the_owning_modules_are_listed() -> None:
    assert OWNED_SETTINGS_MODULES == (
        "tai42_skeleton.access_control.settings",
        "tai42_skeleton.interactions.settings",
        "tai42_skeleton.channels.settings",
        "tai42_kit.llm.settings",
    )


def test_boot_passes_with_only_known_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    _write_dotenv(tmp_path, {"INTERACTIONS_KEY_PREFIX": "x:"})
    require_known_owned_settings()
    refuse_unknown_owned_applied_env(["CHANNELS_NOTIFICATIONS_FEED_MAX", "OTHER"])


def test_boot_refuses_an_unknown_name_in_the_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_CONTROL_ENABEL", "true")
    with pytest.raises(UnknownOwnedSettingError, match=r"^boot \(process environment\): .*ACCESS_CONTROL_ENABEL"):
        require_known_owned_settings()


def test_boot_refuses_an_unknown_name_in_the_dotenv_file(tmp_path: Path) -> None:
    _write_dotenv(tmp_path, {"ACCESS_CONTROL_ENABEL": "true"})
    with pytest.raises(UnknownOwnedSettingError, match=r"^boot \(\.env file\): .*ACCESS_CONTROL_ENABEL"):
        require_known_owned_settings()


def test_the_applied_env_refuses_an_unknown_name() -> None:
    with pytest.raises(UnknownOwnedSettingError, match=r"^stored env: .*ACCESS_CONTROL_ENABEL"):
        refuse_unknown_owned_applied_env(["ACCESS_CONTROL_ENABEL"])


def test_a_service_link_family_in_the_process_env_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    for key, value in _LINK_FAMILY.items():
        monkeypatch.setenv(key, value)
    require_known_owned_settings()


def test_the_same_names_in_the_dotenv_file_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _write_dotenv(tmp_path, _LINK_FAMILY)
    with pytest.raises(UnknownOwnedSettingError, match=r"^boot \(\.env file\): .*ACCESS_CONTROL_PORT \(prefix"):
        require_known_owned_settings()


def test_the_same_names_in_the_applied_env_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with pytest.raises(UnknownOwnedSettingError, match=r"^stored env: .*ACCESS_CONTROL_SERVICE_HOST"):
        refuse_unknown_owned_applied_env(_LINK_FAMILY)


@pytest.mark.parametrize("in_pod", [True, False])
def test_a_lone_mistyped_port_setting_refuses(monkeypatch: pytest.MonkeyPatch, in_pod: bool) -> None:
    if in_pod:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("ACCESS_CONTROL_REDIS_PORT", "6380")
    with pytest.raises(UnknownOwnedSettingError, match="ACCESS_CONTROL_REDIS_PORT"):
        require_known_owned_settings()


def test_a_lone_port_link_name_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("ACCESS_CONTROL_PORT", "tcp://10.0.0.2:8000")
    with pytest.raises(UnknownOwnedSettingError, match="ACCESS_CONTROL_PORT"):
        require_known_owned_settings()


def test_an_env_write_setting_an_unknown_name_refuses() -> None:
    with pytest.raises(UnknownOwnedSettingError, match=r"^env write: .*INTERACTIONS_TYPO \(prefix INTERACTIONS_\)"):
        refuse_unknown_owned_env_write(["INTERACTIONS_TYPO", "INTERACTIONS_KEY_PREFIX"])


def test_app_context_refuses_boot_on_an_unknown_owned_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    monkeypatch.setenv("ACCESS_CONTROL_ENABEL", "true")

    async def boot() -> None:
        async with app.app_context(Manifest.model_validate({})):
            pass

    with pytest.raises(UnknownOwnedSettingError, match="ACCESS_CONTROL_ENABEL"):
        asyncio.run(boot())


def test_an_unknown_llm_provider_checkpoint_setting_refuses_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_TTL_MINUTES", "43200")
    with pytest.raises(
        UnknownOwnedSettingError,
        match=r"^boot \(process environment\): .*LLM_PROVIDER_CHECKPOINT_TTL_MINUTES \(prefix LLM_PROVIDER_\)",
    ):
        require_known_owned_settings()


def test_app_context_refuses_boot_on_an_unknown_llm_provider_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_TTL_MINUTES", "43200")

    async def boot() -> None:
        async with app.app_context(Manifest.model_validate({})):
            pass

    with pytest.raises(UnknownOwnedSettingError, match="LLM_PROVIDER_CHECKPOINT_TTL_MINUTES"):
        asyncio.run(boot())


def test_an_env_write_setting_an_unknown_llm_provider_name_refuses() -> None:
    with pytest.raises(
        UnknownOwnedSettingError,
        match=r"^env write: .*LLM_PROVIDER_CHECKPOINT_TTL_MINUTES \(prefix LLM_PROVIDER_\)",
    ):
        refuse_unknown_owned_env_write(["LLM_PROVIDER_CHECKPOINT_TTL_MINUTES", "LLM_PROVIDER_CHECKPOINT"])


def test_the_llm_provider_settings_pass_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "2880")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", "60")
    require_known_owned_settings()


def test_an_interactions_name_of_a_channel_setting_refuses_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERACTIONS_NOTIFICATIONS_FEED_MAX", "5")
    with pytest.raises(UnknownOwnedSettingError, match="INTERACTIONS_NOTIFICATIONS_FEED_MAX"):
        require_known_owned_settings()


def test_the_channel_settings_read_their_own_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.channels.settings import ChannelsSettings

    monkeypatch.setenv("CHANNELS_NOTIFICATIONS_FEED_MAX", "5")
    monkeypatch.setenv("CHANNELS_NOTIFICATIONS_FEED_TTL_SECONDS", "60")
    monkeypatch.setenv("CHANNELS_SEND_RECEIPT_INDEX_TTL_SECONDS", "30")
    require_known_owned_settings()
    settings = ChannelsSettings()
    assert (
        settings.notifications_feed_max,
        settings.notifications_feed_ttl_seconds,
        settings.send_receipt_index_ttl_seconds,
    ) == (5, 60, 30)


@pytest.mark.usefixtures("_restore_applied_env")
def test_boot_refuses_an_unknown_name_in_the_stored_env_of_a_non_file_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    monkeypatch.setattr(app, "_config_manager", _StandInManager({"ACCESS_CONTROL_ENABEL": "true"}))

    async def boot() -> None:
        app._apply_stored_env_at_boot()
        async with app.app_context(Manifest.model_validate({})):
            pass

    with pytest.raises(UnknownOwnedSettingError, match=r"^stored env: .*ACCESS_CONTROL_ENABEL"):
        asyncio.run(boot())


@pytest.mark.usefixtures("_restore_applied_env")
def test_a_reload_refuses_an_unknown_owned_name_in_the_env_it_applies() -> None:
    from tai42_skeleton.app import epoch as epoch_mod
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    from ..app._fixtures.reload import reload_with

    manifest = Manifest.model_validate({})

    async def run() -> None:
        async with app.app_context(manifest):
            live = epoch_mod.current_epoch()
            with pytest.raises(UnknownOwnedSettingError, match=r"^stored env: .*ACCESS_CONTROL_ENABEL"):
                await reload_with(app, manifest, {"ACCESS_CONTROL_ENABEL": "true"})
            # The refused build is discarded: the live epoch keeps serving under the old env.
            assert epoch_mod.current_epoch() is live
            assert "ACCESS_CONTROL_ENABEL" not in os.environ
            # An env without the unknown name reloads.
            await reload_with(app, manifest, {"INTERACTIONS_KEY_PREFIX": "x:"})
            assert epoch_mod.current_epoch() is not live

    asyncio.run(run())


@pytest.mark.usefixtures("_restore_applied_env")
def test_a_config_reload_refuses_an_unknown_name_read_from_a_non_file_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app import epoch as epoch_mod
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    store = _StandInManager({})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            live = epoch_mod.current_epoch()
            monkeypatch.setattr(app, "_config_manager", store)
            # The store gains a mistyped owned name outside every env-write door.
            store._env = {"ACCESS_CONTROL_ENABEL": "true"}
            with pytest.raises(UnknownOwnedSettingError, match=r"^stored env: .*ACCESS_CONTROL_ENABEL"):
                await asyncio.to_thread(app._reload_config)
            assert epoch_mod.current_epoch() is live

    asyncio.run(run())
