"""The expanded-read dangling guard (cold-boot + offline CLI validation) and the
``get_mcp_env_refs`` names-only projection.

A bare ``!ENV ${VAR}`` (no ``:default``) resolves to the literal ``"N/A"`` when its var
is absent — a silent phantom. The guard closes that path at every expanded read
(``read_manifest`` / ``read_defaults_manifest``) and on the offline CLI validate path,
raising loudly naming each ``(var, json-pointer)``; ``get_mcp_env_refs`` surfaces the
same marker refs as a names/booleans-only checklist (never a value).
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.config.file_manager import FileConfigManager

_DANGLING = 'db_url: !ENV "${M34_MISSING_VAR}"\n'
_DEFAULTED = 'db_url: !ENV "${M34_MISSING_VAR:sqlite://x}"\n'


def _manager(tmp_path, monkeypatch) -> FileConfigManager:
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("M34_MISSING_VAR", raising=False)
    return FileConfigManager(config_dir_path=str(tmp_path))


def test_read_manifest_refuses_a_dangling_required_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.yml").write_text(_DANGLING, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=r"M34_MISSING_VAR.*/db_url"):
        mgr.read_manifest()


def test_read_manifest_boots_a_resolvable_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.yml").write_text(_DANGLING, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    monkeypatch.setenv("M34_MISSING_VAR", "postgres://live")
    assert mgr.read_manifest()["db_url"] == "postgres://live"


def test_read_manifest_boots_a_default_bearing_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``${VAR:default}`` never dangles — an absent var resolves to the default.
    (tmp_path / "manifest.yml").write_text(_DEFAULTED, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    assert mgr.read_manifest()["db_url"] == "sqlite://x"


def test_read_defaults_manifest_refuses_a_dangling_required_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text(_DANGLING, encoding="utf-8")
    mgr = _manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=r"M34_MISSING_VAR.*/db_url"):
        mgr.read_defaults_manifest()


def test_cli_validate_refuses_a_dangling_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    from tai42_skeleton.cli.commands._common import validate_manifest_file

    monkeypatch.delenv("M34_MISSING_VAR", raising=False)
    path = tmp_path / "manifest.yml"
    path.write_text(_DANGLING, encoding="utf-8")
    with pytest.raises(typer.BadParameter, match=r"M34_MISSING_VAR.*/db_url"):
        validate_manifest_file(str(path))


def test_cli_validate_passes_a_resolvable_manifest(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.cli.commands._common import validate_manifest_file

    monkeypatch.delenv("M34_MISSING_VAR", raising=False)
    path = tmp_path / "manifest.yml"
    # A default-bearing marker on a real (schema-valid) field: the guard passes (the
    # default resolves) and Manifest.model_validate accepts it.
    path.write_text('storage_module: !ENV "${M34_MISSING_VAR:tai42_storage_local.storage}"\n', encoding="utf-8")
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
                        "PGPASSWORD": "!ENV ${M34_PG_PW}",
                        "PGHOST": "!ENV ${M34_PG_HOST:localhost}",
                    },
                },
            }
        ],
        "user_tools": [],
    }
    monkeypatch.setattr(ops, "_preserved_manifest_view", lambda: view)
    monkeypatch.setenv("M34_PG_PW", "s3cret")
    monkeypatch.delenv("M34_PG_HOST", raising=False)

    refs = asyncio.run(ops.get_mcp_env_refs())
    by_var = {ref["var"]: ref for ref in refs}

    assert by_var["M34_PG_PW"]["has_default"] is False
    assert by_var["M34_PG_PW"]["set"] is True
    assert by_var["M34_PG_PW"]["pointer"].startswith("/mcp/0/config/env/")

    assert by_var["M34_PG_HOST"]["has_default"] is True
    # A var supplied only by the deployment env would show green; here it is unset
    # AND carries a default, so its checklist entry is not required.
    assert by_var["M34_PG_HOST"]["set"] is False

    # Names and booleans ONLY — never a value.
    for ref in refs:
        assert set(ref) == {"var", "pointer", "has_default", "set"}
