"""The boot-time check of the platform features a loaded plugin requires.

The check itself (:func:`refuse_unmet_plugin_requirements`) is driven with neutral specs; its
doors are driven with neutral plugin distributions laid out on disk whose packaged spec
declares ``requires``: every epoch build (process boot and reload) refuses before any module
of the plugin imports, and ``tai setup --recover`` refuses before any lifecycle module
imports. A plugin is checked however the deployment loads it: through a manifest module,
through ``studio_plugins``, or as the config provider ``TAI_CONFIG_MODE`` selects. Each
required name resolves through its gated feature's own ``configured`` predicate, so the tests
turn a feature on the way an operator does: by its enabling env var.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import database_password_env
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.epoch import current_epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.kind_status import gated_features
from tai42_skeleton.app.lifecycle.plugin_requirements import refuse_unmet_plugin_requirements
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import CorePluginBootError

from ...db import _chain_plugin as probe

# The env var that turns the skeleton database — and so ``versioning`` and ``tool_meta`` — on.
_DB_PASSWORD = database_password_env("default")
_REFUSAL = "a loaded plugin requires a platform feature this deployment does not provide — refusing to start: "
_REMEDY = (
    ". Turn the feature on, or stop loading the plugin: remove it from the manifest, "
    "or set TAI_CONFIG_MODE to another provider when it is the config provider."
)
_PROBE_REF = "tai42/chainprobe"


def _spec(name: str, requires: list[str]) -> PluginSpec:
    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "acme",
            "name": name,
            "package": f"acme-{name}",
            "version": "1.0.0",
            "description": "A neutral test plugin.",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1000",
            "categories": ["dev"],
            "provides": [{"kind": "tool", "name": "ping", "module": f"acme_{name}.tools", "description": "d"}],
            "requires": requires,
        }
    )


def _probe_spec_yaml(requires: list[str]) -> str:
    return (
        "spec_version: 1\n"
        "namespace: tai42\n"
        "name: chainprobe\n"
        f"package: {probe.DIST}\n"
        "version: 1.0.0\n"
        "description: A neutral feature-requiring test plugin\n"
        "license: Apache-2.0\n"
        "contract: '>=0.1,<1000'\n"
        "categories: [dev]\n"
        "provides:\n"
        "  - kind: tool\n"
        "    name: chainprobe_ping\n"
        f"    module: {probe.TOOLS_MODULE}\n"
        "    description: d\n"
        f"requires: [{', '.join(requires)}]\n"
    )


def _studio_probe_spec_yaml(requires: list[str]) -> str:
    return (
        "spec_version: 1\n"
        "namespace: tai42\n"
        "name: chainprobe\n"
        f"package: {probe.DIST}\n"
        "version: 1.0.0\n"
        "description: A neutral feature-requiring Studio plugin\n"
        "license: Apache-2.0\n"
        "contract: '>=0.1,<1000'\n"
        "categories: [dev]\n"
        "provides:\n"
        "  - kind: studio-plugin\n"
        "    name: chainprobe_panel\n"
        f"    module: {probe.TOP_LEVEL}\n"
        "    description: d\n"
        f"requires: [{', '.join(requires)}]\n"
    )


# A neutral config provider, selected by ``TAI_CONFIG_MODE=acmecfg``.
_CONFIG_MODE = "acmecfg"
_CONFIG_DIST = "tai42-config-acmecfg"
_CONFIG_TOP_LEVEL = "tai42_config_acmecfg"
_CONFIG_REF = "acme/config-acmecfg"


def _lay_out_config_provider(site: Path, requires: list[str]) -> None:
    """Write the config provider distribution into ``site`` the way ``pip`` leaves one."""
    package = site / _CONFIG_TOP_LEVEL
    package.mkdir(parents=True)
    files = {
        package / "__init__.py": "",
        package / "manager.py": "",
        package / "tai-plugin.yml": (
            "spec_version: 1\n"
            "namespace: acme\n"
            "name: config-acmecfg\n"
            f"package: {_CONFIG_DIST}\n"
            "version: 1.0.0\n"
            "description: A neutral feature-requiring config provider\n"
            "license: Apache-2.0\n"
            "contract: '>=0.1,<1000'\n"
            "categories: [dev]\n"
            "provides:\n"
            "  - kind: config\n"
            f"    name: {_CONFIG_MODE}\n"
            f"    module: {_CONFIG_TOP_LEVEL}.manager\n"
            "    description: d\n"
            f"requires: [{', '.join(requires)}]\n"
        ),
    }
    for path, body in files.items():
        path.write_text(body, encoding="utf-8")
    dist_info = site / f"{_CONFIG_DIST.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {_CONFIG_DIST}\nVersion: 1.0.0\n", encoding="utf-8"
    )
    rows = [f"{path.relative_to(site).as_posix()},," for path in files]
    rows += [f"{dist_info.name}/METADATA,,", f"{dist_info.name}/RECORD,,"]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _manifest_loading_probe() -> Manifest:
    return Manifest.model_validate(
        {"default_routers": "none", "tools": [{"title": "Chainprobe", "module": probe.TOOLS_MODULE}]}
    )


def _feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_DB_PASSWORD, raising=False)
    reset_all_settings()


def _feature_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_DB_PASSWORD, "pw")
    reset_all_settings()


@pytest.fixture(autouse=True)
def _restore_process_env() -> Iterator[None]:
    """A successful build+swap leaves its applied env live in ``os.environ``; restore it."""
    from tai42_skeleton.app import applied_env

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = set()
    reset_all_settings()


@pytest.fixture
def requiring_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The neutral plugin, requiring ``versioning``, installed on an isolated ``sys.path``."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    site = tmp_path / "site"
    probe.lay_out(site, spec_text=_probe_spec_yaml(["versioning"]))
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    yield
    for name in [name for name in sys.modules if name.partition(".")[0] == probe.TOP_LEVEL]:
        del sys.modules[name]
    importlib.invalidate_caches()


@pytest.fixture
def requiring_studio_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The neutral plugin as a Studio plugin only, requiring ``versioning``, on an isolated ``sys.path``."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    site = tmp_path / "site"
    probe.lay_out(site, spec_text=_studio_probe_spec_yaml(["versioning"]))
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    yield
    for name in [name for name in sys.modules if name.partition(".")[0] == probe.TOP_LEVEL]:
        del sys.modules[name]
    importlib.invalidate_caches()


@pytest.fixture
def requiring_config_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The neutral config provider, requiring ``versioning``, installed and selected by ``TAI_CONFIG_MODE``."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    site = tmp_path / "site"
    _lay_out_config_provider(site, ["versioning"])
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    monkeypatch.setenv("TAI_CONFIG_MODE", _CONFIG_MODE)
    reset_all_settings()
    yield
    for name in [name for name in sys.modules if name.partition(".")[0] == _CONFIG_TOP_LEVEL]:
        del sys.modules[name]
    importlib.invalidate_caches()


# --- the check ----------------------------------------------------------------


def test_a_plugin_requiring_nothing_passes_with_every_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_off(monkeypatch)

    refuse_unmet_plugin_requirements([_spec("relay", [])])


def test_a_required_feature_that_is_on_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_on(monkeypatch)

    refuse_unmet_plugin_requirements([_spec("relay", ["versioning", "tool_meta"])])


def test_a_required_feature_that_is_off_refuses_naming_its_enabling_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_off(monkeypatch)

    with pytest.raises(CorePluginBootError) as excinfo:
        refuse_unmet_plugin_requirements([_spec("relay", ["versioning"])])

    assert str(excinfo.value) == (
        f"{_REFUSAL}plugin 'acme/relay' requires the Versioning feature ('versioning'), "
        f"which is off: set {_DB_PASSWORD}{_REMEDY}"
    )


def test_an_unknown_name_refuses_naming_every_known_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_on(monkeypatch)

    with pytest.raises(CorePluginBootError) as excinfo:
        refuse_unmet_plugin_requirements([_spec("relay", ["teleport"])])

    known = ", ".join(sorted(gated_features()))
    assert str(excinfo.value) == (
        f"{_REFUSAL}plugin 'acme/relay' requires 'teleport', which is not a platform feature "
        f"(known features: {known}){_REMEDY}"
    )


def test_one_refusal_names_every_unmet_requirement_of_every_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_off(monkeypatch)
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)

    with pytest.raises(CorePluginBootError) as excinfo:
        refuse_unmet_plugin_requirements([_spec("relay", ["versioning", "teleport"]), _spec("echo", ["tool_runs"])])

    message = str(excinfo.value)
    assert "plugin 'acme/relay' requires the Versioning feature ('versioning')" in message
    assert "plugin 'acme/relay' requires 'teleport', which is not a platform feature" in message
    assert "plugin 'acme/echo' requires the Background tool runs feature ('tool_runs'), " in message
    assert "set TAI_TOOL_RUNS_REDIS_URL" in message


def test_each_name_resolves_through_its_features_own_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The check reads the gated feature's ``configured`` predicate, the one the kinds row reads:
    # whatever that predicate answers is the verdict.
    feature = gated_features()["states"]
    answers: list[bool] = []

    def configured() -> bool:
        answers.append(False)
        return False

    monkeypatch.setattr(
        "tai42_skeleton.app.lifecycle.plugin_requirements.gated_features",
        lambda: {"states": replace(feature, configured=configured)},
    )

    with pytest.raises(CorePluginBootError, match="'states'"):
        refuse_unmet_plugin_requirements([_spec("relay", ["states"])])
    assert answers == [False]


# --- the doors ----------------------------------------------------------------


@pytest.mark.usefixtures("requiring_probe")
def test_boot_refuses_before_importing_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_off(monkeypatch)

    async def run() -> None:
        with pytest.raises(CorePluginBootError) as excinfo:
            async with app.app_context(_manifest_loading_probe()):
                pass
        assert f"plugin {_PROBE_REF!r} requires the Versioning feature ('versioning')" in str(excinfo.value)
        assert f"set {_DB_PASSWORD}" in str(excinfo.value)

    asyncio.run(run())
    assert probe.TOOLS_MODULE not in sys.modules
    assert probe.TOP_LEVEL not in sys.modules


@pytest.mark.usefixtures("requiring_probe")
def test_boot_loads_the_plugin_when_its_required_feature_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_on(monkeypatch)

    async def run() -> None:
        async with app.app_context(_manifest_loading_probe()):
            assert probe.TOOLS_MODULE in sys.modules

    asyncio.run(run())


@pytest.mark.usefixtures("requiring_probe")
def test_reload_refuses_and_the_live_epoch_keeps_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    _feature_off(monkeypatch)
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: _manifest_loading_probe().model_dump())
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            live = current_epoch()
            manifest_before = app._manifest
            with pytest.raises(CorePluginBootError, match=f"plugin {_PROBE_REF!r} requires the Versioning feature"):
                await reload_gate.run(app.admin.reload_config, reimports=True)
            assert current_epoch() is live
            assert app._manifest is manifest_before

    asyncio.run(run())
    assert probe.TOOLS_MODULE not in sys.modules
    assert probe.TOP_LEVEL not in sys.modules


@pytest.mark.usefixtures("requiring_studio_probe")
def test_boot_refuses_a_studio_plugin_requiring_an_off_feature_before_importing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature_off(monkeypatch)
    manifest = Manifest.model_validate({"default_routers": "none", "studio_plugins": [probe.TOP_LEVEL]})

    async def run() -> None:
        with pytest.raises(CorePluginBootError) as excinfo:
            async with app.app_context(manifest):
                pass
        assert f"plugin {_PROBE_REF!r} requires the Versioning feature ('versioning')" in str(excinfo.value)
        assert f"set {_DB_PASSWORD}" in str(excinfo.value)

    asyncio.run(run())
    assert probe.TOP_LEVEL not in sys.modules


@pytest.mark.usefixtures("requiring_config_provider")
def test_the_preflight_refuses_a_config_provider_requiring_an_off_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    # The config provider is selected by TAI_CONFIG_MODE and loaded by every deployment, whatever
    # its manifest names; the preflight every epoch build and ``tai setup --recover`` run reads it.
    from tai42_skeleton.app.lifecycle.manifest_imports import prepare_manifest_imports

    _feature_off(monkeypatch)

    with pytest.raises(CorePluginBootError) as excinfo:
        asyncio.run(prepare_manifest_imports(Manifest.model_validate({"default_routers": "none"})))

    assert f"plugin {_CONFIG_REF!r} requires the Versioning feature ('versioning')" in str(excinfo.value)
    assert f"set {_DB_PASSWORD}" in str(excinfo.value)
    assert str(excinfo.value).endswith(_REMEDY)


@pytest.mark.usefixtures("requiring_config_provider")
def test_the_preflight_passes_a_config_provider_whose_required_feature_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app.lifecycle.manifest_imports import prepare_manifest_imports

    _feature_on(monkeypatch)
    monkeypatch.setattr(
        "tai42_skeleton.app.lifecycle.manifest_imports.assert_deployment_schemas_applied",
        _no_schema_gate,
    )

    asyncio.run(prepare_manifest_imports(Manifest.model_validate({"default_routers": "none"})))


async def _no_schema_gate(manifest: Manifest, installed: object) -> None:
    """Stand in for the schema gate, which needs a live database once the database feature is on."""
