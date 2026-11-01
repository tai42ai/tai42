"""``tai plugins uninstall --offline`` — uninstall one plugin on the deployment host with no server.

The boot read is faked; the installer runs on the installer harness's fakes (the install store,
pip, the config pipeline and a recording fleet lock), wired with the pipeline
``ConfigService.host_side()`` returns and the convergence the command passes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner, Result
from tai42_cli import app as app_module
from tests.marketplace._specs import make_spec
from tests.marketplace.test_installer import Harness, layer_inputs

from tai42_skeleton.app import instance
from tai42_skeleton.cli.native import plugins_uninstall
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.marketplace.environments.settings import PLUGINS_DIR_UNSET
from tai42_skeleton.marketplace.errors import PipFailedError
from tai42_skeleton.marketplace.manifest_apply import converge_at_next_start
from tai42_skeleton.marketplace.unwind import ENVIRONMENT_RESTORED

_REF = "tai42/toolbox"
_TOOL = {"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}


def _harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """A harness holding the toolbox row and its manifest entry, served to the command."""
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    h.store.preload(make_spec(provides=[_TOOL]), version="1.0.0")

    class _Lifecycle:
        async def read_boot_manifest(self) -> None:
            h.events.append("boot:read")

    def _installer(*, config_service: Any, converge: Any) -> Any:
        assert config_service is h.svc
        assert converge is converge_at_next_start
        return h.installer(converge=converge)

    monkeypatch.setattr(instance, "app", SimpleNamespace(lifecycle=_Lifecycle()), raising=False)
    monkeypatch.setattr(ConfigService, "host_side", lambda: h.svc)
    monkeypatch.setattr(plugins_uninstall, "Installer", _installer)
    return h


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(app_module.app, [*args, "plugins", "uninstall", "--offline", _REF])


def _effects(h: Harness) -> list[str]:
    kept = ("boot:read", "lock:acquire", "store:get", "cm:write", "reload", "store:delete", "lock:release")
    return [event for event in h.events if event in kept]


def test_an_offline_uninstall_strips_the_plugin_and_says_a_server_loads_it_on_its_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness(monkeypatch)

    result = _invoke("--json")

    assert result.exit_code == 0, result.output
    assert _effects(h) == ["boot:read", "lock:acquire", "store:get", "cm:write", "store:delete", "lock:release"]
    payload = json.loads(result.output)
    assert payload["ref"] == _REF
    assert payload["uninstalled"] is True
    assert payload["manifest_written"] is True
    assert payload["notes"][-1] == plugins_uninstall.NEXT_START
    assert _REF not in h.store.rows


def test_an_offline_uninstall_prints_the_next_start_note_last(monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert _REF in result.output
    assert plugins_uninstall.NEXT_START in result.output


def test_an_offline_uninstall_of_an_unknown_ref_exits_non_zero_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _harness(monkeypatch)
    h.store.rows.clear()

    result = _invoke()

    assert result.exit_code == 1
    assert f"{_REF} is not installed" in result.output
    assert "store:delete" not in h.events


def test_an_offline_uninstall_whose_layer_still_conflicts_exits_non_zero_and_keeps_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness(monkeypatch)
    h.store.preload(
        make_spec(name="other", package="tai42-other", provides=[{**_TOOL, "name": "o", "module": "o.tools"}]),
        version="2.0.0",
    )

    async def conflicting_pip(args: list[str], /, *, env: Any = None) -> str:
        raise PipFailedError(args, 1, "ERROR: ResolutionImpossible: tai42-other pins tai42-contract==0.0.1")

    h.pip = conflicting_pip  # type: ignore[assignment]
    before = layer_inputs(h)

    result = _invoke()

    assert result.exit_code == 1
    assert "its dependencies conflict with the installed plugins or this image" in result.output
    assert layer_inputs(h) == before
    assert _REF in h.store.rows
    assert "cm:write" not in h.events


def test_an_offline_uninstall_whose_manifest_write_is_refused_says_the_environment_was_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness(monkeypatch)
    h.svc.raise_on_validate = ValueError("manifest refused by an invariant")

    result = _invoke()

    assert result.exit_code == 1
    assert "manifest refused by an invariant" in result.output
    assert ENVIRONMENT_RESTORED in result.output
    assert _REF in h.store.rows


def test_an_offline_uninstall_while_another_operation_holds_the_lock_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness(monkeypatch)
    h.fleet.held = True

    result = _invoke()

    assert result.exit_code == 1
    assert "another marketplace operation is in progress; retry shortly" in result.output
    assert _REF in h.store.rows


def test_an_offline_uninstall_with_the_plugins_directory_unset_exits_non_zero_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness(monkeypatch)
    h.plugins_dir = None

    result = _invoke()

    assert result.exit_code == 1
    assert PLUGINS_DIR_UNSET in result.output


def test_an_offline_uninstall_whose_config_provider_is_absent_exits_non_zero_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _absent_provider() -> Any:
        raise ImportError("config mode 'vault' needs the 'tai42_config_vault' provider (install tai42-config-vault)")

    monkeypatch.delattr(instance, "app", raising=False)
    monkeypatch.setattr(instance, "build_app", _absent_provider)

    result = _invoke()

    assert result.exit_code == 1
    assert "needs the 'tai42_config_vault' provider" in result.output


def test_the_offline_uninstall_handler_is_registered_at_cli_load() -> None:
    from tai42_cli.commands import plugins as plugins_cmd

    assert plugins_cmd._offline_uninstall is plugins_uninstall.uninstall
