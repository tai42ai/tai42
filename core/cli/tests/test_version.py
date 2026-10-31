"""``tai version`` — enumerates installed tai42 distributions plus key CLI deps."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tai42_cli import app as app_module
from tai42_cli import version as version_mod


def test_versions_lists_installed_tai_packages_and_extras() -> None:
    records = version_mod._versions()
    by_name = {row["package"]: row["version"] for row in records}
    # tai42-cli is always installed here; its version is a real string, not absent.
    assert "tai42-cli" in by_name
    assert by_name["tai42-cli"] != "not installed"
    # The tai42-* block is sorted and precedes the extra deps.
    tai_names = [row["package"] for row in records if row["package"].startswith("tai42-")]
    assert tai_names == sorted(tai_names)
    for extra in ("typer", "click", "httpx"):
        assert extra in by_name


def test_version_json_lists_tai_cli() -> None:
    result = CliRunner().invoke(app_module.app, ["--json", "version"])
    assert result.exit_code == 0, result.output
    names = {row["package"]: row["version"] for row in json.loads(result.output)}
    assert names["tai42-cli"] != "not installed"


def test_version_marks_absent_extra_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An extra dependency that is not installed reports so, never a raised error.
    monkeypatch.setattr(version_mod, "_EXTRA_PACKAGES", ["definitely-not-a-real-package-xyz"])
    records = version_mod._versions()
    by_name = {row["package"]: row["version"] for row in records}
    assert by_name["definitely-not-a-real-package-xyz"] == "not installed"
    # The discovered tai42-* distributions are unaffected.
    assert any(name.startswith("tai42-") for name in by_name)


def test_version_names_a_distribution_recording_no_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A stray .dist-info with no METADATA is a clean failure naming its path, never a traceback.
    stray = tmp_path / "site" / "stray-1.0.0.dist-info"
    stray.mkdir(parents=True)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    importlib.invalidate_caches()

    result = CliRunner().invoke(app_module.app, ["version"])

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert f"the installed distribution metadata at {stray} records no Name" in result.output
