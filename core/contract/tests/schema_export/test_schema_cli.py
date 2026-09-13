"""The ``tai42-contract-schemas`` command: it writes the bundle, and its ``--check``
mode reds the build when the committed copy drifts from a fresh rebuild.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tai42_contract.schema_export import build_document_schemas, bundle_json, cli


def test_check_passes_when_committed_is_fresh(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--check"]) == 0
    assert "fresh" in capsys.readouterr().err


def test_check_reds_and_names_drift_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = build_document_schemas()
    stale["documents"].pop("StateTemplateDocument")
    stale_file = tmp_path / "contract-schema.json"
    stale_file.write_text(bundle_json(stale), encoding="utf-8")
    monkeypatch.setattr(cli, "committed_bundle_path", lambda: stale_file)

    assert cli.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "stale" in err
    assert "StateTemplateDocument" in err


def test_out_writes_the_canonical_bundle(tmp_path: Path) -> None:
    out = tmp_path / "bundle.json"
    assert cli.main(["--out", str(out)]) == 0
    written = out.read_text(encoding="utf-8")
    assert written == bundle_json(build_document_schemas())
    assert json.loads(written)["contract_version"]
