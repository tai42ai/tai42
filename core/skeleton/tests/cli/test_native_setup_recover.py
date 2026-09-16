"""``tai setup --recover`` — the host-side owner-key recovery command.

No live stores: the deployment read (the boot-manifest read and the lifecycle-module import)
and the recovery orchestration are faked, so the tests assert the command wiring — it stamps
the resolved manifest path, reads the manifest, imports the lifecycle modules, renders the
recovery result, and surfaces a recovery refusal and a missing manifest path as loud,
non-zero failures.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import psycopg
import pytest
from click.testing import CliRunner
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_cli import app as app_module

import tai42_skeleton.app.instance as instance
from tai42_skeleton.access_control.setup_recovery import SetupRecoveryError
from tai42_skeleton.cli.native import setup_recover

_RESULT = {
    "owner_user_id": "owner",
    "key_user_id": "owner-key",
    "api_key": "sk-owner-key",
    "key_fingerprint": "fp-1",
}


@pytest.fixture
def fake_deployment(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake the boot-manifest read and record the manifest path stamped before it, plus the imports."""
    seen: dict = {"imported": []}

    class _FakeLifecycle:
        def read_boot_manifest(self) -> SimpleNamespace:
            seen["manifest_env"] = os.environ.get("TAI_MANIFEST_PATH")
            return SimpleNamespace(lifecycle_modules=["deployment.lifecycle.module"])

    monkeypatch.setattr(instance, "app", SimpleNamespace(lifecycle=_FakeLifecycle()))
    monkeypatch.setattr(setup_recover.importlib, "import_module", lambda name: seen["imported"].append(name))
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


def test_recover_activates_the_prefix_before_importing_lifecycle_modules(
    monkeypatch: pytest.MonkeyPatch, fake_deployment: dict
) -> None:
    import tai42_skeleton.marketplace.prefix as prefix

    calls: list[str] = []
    monkeypatch.setattr(prefix, "activate_prefix", lambda: calls.append("activate_prefix"))
    monkeypatch.setattr(setup_recover.importlib, "import_module", lambda name: calls.append(f"import:{name}"))

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
