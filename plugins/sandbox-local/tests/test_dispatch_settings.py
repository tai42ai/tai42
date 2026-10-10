"""The provider's own ``SANDBOX_LOCAL_`` group is its dispatch settings, and it claims its env prefix."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tai42_kit.settings import UnknownOwnedSettingError, refuse_unknown_owned_env
from tai42_kit.settings import registry as registry_mod

from tai42_sandbox_local.settings import SandboxLocalSettings


@pytest.fixture
def _only_the_provider_group() -> Iterator[None]:
    saved = dict(registry_mod._REGISTRY)
    registry_mod._clear_registry()
    registry_mod._register(SandboxLocalSettings)
    yield
    registry_mod._clear_registry()
    registry_mod._REGISTRY.update(saved)


def test_the_provider_exposes_its_groups_exec_default_and_reap_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_sandbox_local import provider as provider_mod

    settings = SandboxLocalSettings(exec_default_timeout_seconds=60, reap_interval_seconds=7)
    monkeypatch.setattr(provider_mod.LocalSandbox, "_settings", lambda self: settings)
    sandbox = provider_mod.LocalSandbox()
    assert sandbox.dispatch_settings() is settings
    assert sandbox.exec_default_timeout_seconds == 60
    assert sandbox.reap_interval_seconds == 7


@pytest.mark.usefixtures("_only_the_provider_group")
def test_an_unknown_name_under_the_prefix_is_refused_naming_it() -> None:
    assert SandboxLocalSettings.env_prefix_owned is True
    with pytest.raises(UnknownOwnedSettingError, match=r"SANDBOX_LOCAL_UNKNOWN_NAME \(prefix SANDBOX_LOCAL_\)"):
        refuse_unknown_owned_env({"SANDBOX_LOCAL_UNKNOWN_NAME": "3600"}, source="boot")


@pytest.mark.usefixtures("_only_the_provider_group")
def test_the_dispatch_settings_pass_the_audit() -> None:
    refuse_unknown_owned_env(
        ["SANDBOX_LOCAL_EXEC_DEFAULT_TIMEOUT_SECONDS", "SANDBOX_LOCAL_REAP_INTERVAL_SECONDS"], source="boot"
    )
