"""The dangling ``!ENV`` marker contract and the ``get_mcp_env_refs`` names-only
projection.

A bare ``!ENV ${VAR}`` (no ``:default``) resolves to the literal ``"N/A"`` when its var
is absent. That is REFUSED where a caller INTRODUCES it — a config write/replace
(:class:`~tai42_skeleton.config.service.ConfigService`, covered in ``test_service`` /
``test_boundary``) and the offline CLI ``validate`` path — raising loudly naming each
``(var, json-pointer)``. But the worker BOOT / runtime read (``read_manifest`` /
``read_defaults_manifest``) TOLERATES a not-yet-resolved marker: the fleet
backup/import/convergence pattern seeds a manifest whose env is supplied AFTER boot, so
refusing at boot would abort a legitimate deferred-env deployment. ``get_mcp_env_refs``
surfaces the same marker refs as a names/booleans-only checklist (never a value).
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.config.file_manager import FileConfigManager

_DANGLING = 'db_url: !ENV "${GUARD_MISSING_VAR}"\n'
_DEFAULTED = 'db_url: !ENV "${GUARD_MISSING_VAR:sqlite://x}"\n'


def _manager(tmp_path, monkeypatch) -> FileConfigManager:
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("GUARD_MISSING_VAR", raising=False)
    return FileConfigManager(config_dir_path=str(tmp_path))


def test_read_manifest_tolerates_a_deferred_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Boot/runtime read: an as-yet-unresolved required marker must NOT abort boot — the
    # fleet import/convergence pattern supplies the env after boot. pyaml_env materializes
    # the absent marker to its literal "N/A" default until the env write lands.
    (tmp_path / "manifest.yml").write_text(_DANGLING, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    assert mgr.read_manifest()["db_url"] == "N/A"  # no raise


def test_read_manifest_boots_a_resolvable_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.yml").write_text(_DANGLING, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    monkeypatch.setenv("GUARD_MISSING_VAR", "postgres://live")
    assert mgr.read_manifest()["db_url"] == "postgres://live"


def test_read_manifest_boots_a_default_bearing_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``${VAR:default}`` never dangles — an absent var resolves to the default.
    (tmp_path / "manifest.yml").write_text(_DEFAULTED, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    assert mgr.read_manifest()["db_url"] == "sqlite://x"


def test_read_defaults_manifest_tolerates_a_deferred_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same boot-read contract as read_manifest: a not-yet-resolved marker in the template
    # defaults does not abort boot (it resolves to "N/A" until the env is supplied).
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text(_DANGLING, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    assert mgr.read_defaults_manifest()["db_url"] == "N/A"  # no raise


def test_cli_validate_refuses_a_dangling_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    from tai42_skeleton.cli.offline import validate_manifest_file

    monkeypatch.delenv("GUARD_MISSING_VAR", raising=False)
    path = tmp_path / "manifest.yml"
    path.write_text(_DANGLING, encoding="utf-8")
    with pytest.raises(typer.BadParameter, match=r"GUARD_MISSING_VAR.*/db_url"):
        validate_manifest_file(str(path))


def test_cli_validate_passes_a_resolvable_manifest(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.cli.offline import validate_manifest_file

    monkeypatch.delenv("GUARD_MISSING_VAR", raising=False)
    path = tmp_path / "manifest.yml"
    # A default-bearing marker on a real (schema-valid) field: the guard passes (the
    # default resolves) and Manifest.model_validate accepts it.
    path.write_text('storage_module: !ENV "${GUARD_MISSING_VAR:tai42_storage_local.storage}"\n', encoding="utf-8")
    validate_manifest_file(str(path))  # no raise


def test_get_mcp_env_refs_projects_names_and_booleans_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.operations import manifest as ops

    view = {
        "mcp": [
            {
                "title": "postgres",
                "config": {
                    "command": "pg-mcp-server",
                    "env": {
                        "PGPASSWORD": "!ENV ${GUARD_PG_PW}",
                        "PGHOST": "!ENV ${GUARD_PG_HOST:localhost}",
                    },
                },
            }
        ],
        "user_tools": [],
    }
    monkeypatch.setattr(ops, "_preserved_manifest_view", lambda: view)
    monkeypatch.setenv("GUARD_PG_PW", "s3cret")
    monkeypatch.delenv("GUARD_PG_HOST", raising=False)

    refs = asyncio.run(ops.get_mcp_env_refs())
    by_var = {ref["var"]: ref for ref in refs}

    assert by_var["GUARD_PG_PW"]["has_default"] is False
    assert by_var["GUARD_PG_PW"]["set"] is True
    assert by_var["GUARD_PG_PW"]["pointer"].startswith("/mcp/0/config/env/")

    assert by_var["GUARD_PG_HOST"]["has_default"] is True
    # A var supplied only by the deployment env would show green; here it is unset
    # AND carries a default, so its checklist entry is not required.
    assert by_var["GUARD_PG_HOST"]["set"] is False

    # Names and booleans ONLY — never a value.
    for ref in refs:
        assert set(ref) == {"var", "pointer", "has_default", "set"}
