"""``tai setup --recover`` — the host-side owner-key recovery command.

No live stores: the deployment read (the boot-manifest read and the lifecycle-module import)
and the recovery orchestration are faked, so the tests assert the command wiring — it stamps
the resolved manifest path, reads the manifest, imports the lifecycle modules, renders the
recovery result, and surfaces a recovery refusal and a missing manifest path as loud,
non-zero failures, and that the schema gate and the plugin-compat check run before any
lifecycle module imports. One test imports a real lifecycle module that registers on an app facet
at import, proving the command binds the deployment's app for the imports and the recovery.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import psycopg
import pytest
from click.testing import CliRunner
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_cli import app as app_module
from tai42_contract.app import tai42_app

import tai42_skeleton.app.instance as instance
from tai42_skeleton.access_control.setup_recovery import SetupRecoveryError
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.cli.native import setup_recover
from tai42_skeleton.manifest import Manifest

_RESULT = {
    "owner_user_id": "owner",
    "key_user_id": "owner-key",
    "api_key": "sk-owner-key",
    "key_fingerprint": "fp-1",
}


_LIFECYCLE_MODULE = "deployment.lifecycle.module"
_real_import_module = importlib.import_module


def _recording_import(record: Callable[[str], object]) -> Callable[..., object]:
    """An ``importlib.import_module`` that records the deployment's lifecycle module instead of importing it.

    ``setup_recover.importlib`` is the process-wide ``importlib`` module, so every other name
    (the platform's own lazy imports) is imported for real.
    """

    def _import(name: str, package: str | None = None) -> object:
        if name != _LIFECYCLE_MODULE:
            return _real_import_module(name, package)
        record(name)
        return SimpleNamespace()

    return _import


@pytest.fixture
def fake_deployment(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake the boot-manifest read and record the manifest path stamped before it, plus the imports."""
    seen: dict = {"imported": []}

    class _FakeLifecycle:
        async def read_boot_manifest(self) -> Manifest:
            seen["manifest_env"] = os.environ.get("TAI_MANIFEST_PATH")
            seen["bridge_loop"] = asyncio.get_running_loop()
            return Manifest.model_validate({"default_routers": "none", "lifecycle_modules": [_LIFECYCLE_MODULE]})

    monkeypatch.setattr(instance, "app", SimpleNamespace(lifecycle=_FakeLifecycle()))
    monkeypatch.setattr(setup_recover.importlib, "import_module", _recording_import(seen["imported"].append))
    return seen


def test_recover_renders_the_result_as_json(monkeypatch: pytest.MonkeyPatch, fake_deployment: dict) -> None:
    seen: dict = {}

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        seen["args"] = (setup_token, key_user_id, key_description)
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app,
        ["--json", "setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["api_key"] == "sk-owner-key"
    assert seen["args"] == ("t", None, "owner key")
    # The path was stamped BEFORE the manifest read, and the manifest's lifecycle modules imported.
    assert fake_deployment["manifest_env"] == "/deploy/manifest.yaml"
    assert fake_deployment["imported"] == ["deployment.lifecycle.module"]


def test_recover_runs_the_bridge_and_the_recovery_on_one_event_loop(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    """The boot bridge is awaited first, and the recovery runs on the loop the bridge ran on."""
    seen: dict = {}

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        seen["bridged_first"] = "bridge_loop" in fake_deployment
        seen["recover_loop"] = asyncio.get_running_loop()
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app,
        ["--json", "setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"],
    )

    assert result.exit_code == 0, result.output
    assert seen["bridged_first"] is True
    assert seen["recover_loop"] is fake_deployment["bridge_loop"]


def test_recover_activates_the_prefix_before_importing_lifecycle_modules(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    import tai42_skeleton.marketplace.prefix as prefix

    calls: list[str] = []
    monkeypatch.setattr(prefix, "activate_prefix", lambda: calls.append("activate_prefix"))
    monkeypatch.setattr(
        setup_recover.importlib, "import_module", _recording_import(lambda name: calls.append(f"import:{name}"))
    )

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app,
        ["--json", "setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"],
    )

    assert result.exit_code == 0, result.output
    # The prefix goes on sys.path before any lifecycle module imports, exactly as at serving boot.
    assert calls == ["activate_prefix", "import:deployment.lifecycle.module"]


def test_recover_refusal_exits_non_zero(monkeypatch: pytest.MonkeyPatch, fake_deployment: dict) -> None:
    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        raise SetupRecoveryError("the owner already holds a live key (owner-key)")

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app,
        ["setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"],
    )

    assert result.exit_code == 1, result.output
    assert "already holds a live key (owner-key)" in result.output


def test_recover_missing_manifest_path_uses_the_serve_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    monkeypatch.setattr(setup_recover, "default_manifest_path", lambda: None)

    result = CliRunner().invoke(app_module.app, ["setup", "--recover", "--token", "t"])

    assert result.exit_code != 0
    assert "A manifest path is required." in result.output


def test_recover_postgres_connection_failure_is_credential_free(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    monkeypatch.setattr(setup_recover, "_pg_target", lambda: "db-host:5432/tai")

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app, ["setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"]
    )

    assert result.exit_code == 1, result.output
    assert "could not connect to Postgres db-host:5432/tai" in result.output


def test_recover_redis_connection_failure_names_the_ac_redis(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        raise RedisConnectionError("no route to host")

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app, ["setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"]
    )

    assert result.exit_code == 1, result.output
    assert "could not connect to the access-control Redis" in result.output


_SHARED_SECRET_MODULE = "tai42_skeleton.webhooks.builtin.shared_secret"


def test_recover_binds_the_app_for_lifecycle_modules_and_the_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lifecycle module that registers on a ``tai42_app`` facet at import loads, and recovery runs bound.

    The command starts from an unbound handle, as a fresh ``tai`` process does. The manifest
    lists the built-in ``shared_secret`` verifier, whose import registers on
    ``tai42_app.webhook_verifiers``; the registration lands on the deployment's app, the
    recovery itself runs with that app bound, and the handle is unbound again afterwards.
    """
    server = TaiMCP(name="recover-under-test")

    async def _read_boot_manifest(*, kind: object) -> Manifest:
        return Manifest.model_validate({"default_routers": "none", "lifecycle_modules": [_SHARED_SECRET_MODULE]})

    monkeypatch.setattr(server, "_read_boot_manifest", _read_boot_manifest)
    monkeypatch.setattr(instance, "app", server)
    # A fresh import, so the module's import-time registration runs inside this command.
    monkeypatch.delitem(sys.modules, _SHARED_SECRET_MODULE, raising=False)
    seen: dict = {}

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        seen["verifier_during_recovery"] = tai42_app.webhook_verifiers.get("shared_secret")
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    with tai42_app.bound(None):
        result = CliRunner().invoke(
            app_module.app,
            ["--json", "setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"],
        )
        assert not hasattr(tai42_app, "webhook_verifiers")

    assert result.exception is None, repr(result.exception)
    assert result.exit_code == 0, result.output
    assert "shared_secret" in server.webhook_verifiers.names()
    assert seen["verifier_during_recovery"] is server.webhook_verifiers.get("shared_secret")


def test_recover_refuses_a_pending_schema_before_importing_lifecycle_modules(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    import tai42_kit.db.migrations as kit_migrations
    from tai42_kit.db import ComponentStatus, MigrationScript

    from tai42_skeleton.db import boot_gate

    pending = MigrationScript(version=2, name="add_thing", filename="0002_add_thing.sql", checksum="c", sql="")

    async def status(entries):
        return [
            ComponentStatus(component=entry.component, applied_versions=(1,), pending=(pending,), mismatches=())
            for entry in entries
        ]

    monkeypatch.setattr(boot_gate, "component_store_configured", lambda component: component == "skeleton")
    monkeypatch.setattr(boot_gate, "component_store_settings", lambda component: SimpleNamespace(pg_dsn="runtime"))
    monkeypatch.setattr(kit_migrations, "migration_status", status)
    recovered: list[str] = []

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        recovered.append(setup_token)
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app, ["setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"]
    )

    assert result.exit_code == 1, result.output
    assert "database schema is out of date — refusing to start: skeleton: 1 pending (0002_add_thing.sql)" in (
        result.output
    )
    assert "Run 'tai db migrate'" in result.output
    assert fake_deployment["imported"] == []
    assert recovered == []


def test_recover_refuses_a_plugin_requiring_an_off_feature_before_importing_lifecycle_modules(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    from tai42_contract.plugins import PluginSpec
    from tai42_kit.db import database_password_env
    from tai42_kit.settings import reset_all_settings

    from tai42_skeleton.app.lifecycle import manifest_imports

    spec = PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "acme",
            "name": "relay",
            "package": "acme-relay",
            "version": "1.0.0",
            "description": "A neutral test plugin.",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1000",
            "categories": ["dev"],
            "provides": [{"kind": "tool", "name": "ping", "module": "deployment.lifecycle.module", "description": "d"}],
            "requires": ["versioning"],
        }
    )
    monkeypatch.setattr(
        manifest_imports, "manifest_plugin_specs", lambda manifest, installed: {"acme/relay": (spec, None)}
    )
    monkeypatch.delenv(database_password_env("default"), raising=False)
    reset_all_settings()
    recovered: list[str] = []

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        recovered.append(setup_token)
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app, ["setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"]
    )

    assert result.exit_code == 1, result.output
    assert "a loaded plugin requires a platform feature this deployment does not provide" in result.output
    assert (
        "plugin 'acme/relay' requires the Versioning feature ('versioning'), "
        f"which is off: set {database_password_env('default')}" in result.output
    )
    assert fake_deployment["imported"] == []
    assert recovered == []


def test_recover_refuses_an_incompatible_lifecycle_module_without_importing_it(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    from tai42_skeleton.marketplace import compat
    from tai42_skeleton.marketplace.installed_packages import InstalledDistribution, InstalledPackages

    read = InstalledDistribution(
        dist=cast(importlib.metadata.Distribution, object()), name="deployment-plugin", recorded=[], top_levels=[]
    )
    monkeypatch.setattr(
        compat,
        "distribution_map",
        lambda: InstalledPackages(
            top_levels={"deployment": ["deployment-plugin"]},
            editable_sources={},
            distributions={"deployment-plugin": read},
        ),
    )
    monkeypatch.setattr(
        compat,
        "distribution_compat",
        lambda read: compat.CompatVerdict("incompatible", f"{read.name} needs another"),
    )

    async def _fake_recover(setup_token: str, *, key_user_id: str | None, key_description: str) -> dict:
        return _RESULT

    monkeypatch.setattr(setup_recover, "recover_owner_key", _fake_recover)

    result = CliRunner().invoke(
        app_module.app, ["setup", "--recover", "--token", "t", "--manifest-path", "/deploy/manifest.yaml"]
    )

    assert result.exit_code == 1, result.output
    assert (
        f"lifecycle plugin {_LIFECYCLE_MODULE!r} is incompatible and cannot load: deployment-plugin needs another"
        in result.output
    )
    assert fake_deployment["imported"] == []
