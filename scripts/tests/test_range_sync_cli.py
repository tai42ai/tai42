"""The command line: exit codes and apply/check stdout tests for scripts/range_sync.py — the shared sample-tree builders
live in _range_sync_support."""

from __future__ import annotations

from pathlib import Path

from _range_sync_support import (
    _build_major_tree,
    _build_tree,
)

import range_sync


def test_check_cli_exit_codes(tmp_path: Path):
    _build_tree(tmp_path)
    # drift present -> exit 1
    assert range_sync.main(["--check", "--root", str(tmp_path)]) == 1
    # apply -> exit 0
    assert range_sync.main(["--root", str(tmp_path)]) == 0
    # now synced -> exit 0
    assert range_sync.main(["--check", "--root", str(tmp_path)]) == 0


def test_check_cli_reports_preserved_and_exits_green(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=True)
    rc = range_sync.main(["--check", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "tai42-contract pinned, left at >=1.2,<2" in out
    assert "WARNING" not in out
    # loud signal, green exit: a preserved pin is not drift
    assert rc == 0


def test_check_cli_warns_on_unpinned_cross_major(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=False)
    rc = range_sync.main(["--check", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "cross-major rewrite of an unannotated cap" in out
    assert "tai42-contract" in out
    # the pending rewrite is genuine drift -> exit 1; the warning itself never drives it
    assert rc == 1


def test_apply_cli_prints_preserved_and_no_warnings(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=True)
    rc = range_sync.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    # the preserved pin is surfaced loudly in apply mode ...
    assert "tai42-contract pinned, left at >=1.2,<2" in out
    # ... while warnings are deliberately suppressed outside --check
    assert "WARNING" not in out
    assert rc == 0


def test_apply_cli_suppresses_warnings_on_unpinned_cross_major(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=False)
    rc = range_sync.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    # the unpinned cross-major cap is synced but its warning is not printed in apply mode
    assert "WARNING" not in out
    assert rc == 0
