"""Unit tests for scripts/check_file_size.py — the source-module line-limit gate.

Hermetic: every case writes synthetic files under a tmp root, so no git runs.
"""

from __future__ import annotations

from pathlib import Path

import check_file_size as cfs  # importable via the scripts/ path conftest.py injects


def _write(path: Path, lines: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join("x = 1" for _ in range(lines)) + "\n", encoding="utf-8")
    return path


def test_count_lines_counts_physical_lines(tmp_path: Path) -> None:
    assert cfs.count_lines(_write(tmp_path / "m.py", 5)) == 5


def test_is_test_file_recognises_test_paths() -> None:
    assert cfs.is_test_file(Path("pkg/tests/test_thing.py"))
    assert cfs.is_test_file(Path("pkg/conftest.py"))
    assert cfs.is_test_file(Path("test_top.py"))
    assert cfs.is_test_file(Path("pkg/thing_test.py"))
    assert not cfs.is_test_file(Path("pkg/src/module.py"))


def test_is_exempt_covers_tests_and_e2e() -> None:
    assert cfs.is_exempt(Path("pkg/tests/test_thing.py"))
    assert cfs.is_exempt(Path("e2e/src/tai42_e2e/pg.py"))
    assert not cfs.is_exempt(Path("pkg/src/module.py"))


def test_source_over_limit_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "big.py", cfs.MAX_SOURCE_LINES + 1)
    oversized = cfs.find_oversized([tmp_path / "src" / "big.py"], tmp_path)
    assert oversized == [(Path("src/big.py"), cfs.MAX_SOURCE_LINES + 1)]


def test_source_at_limit_passes(tmp_path: Path) -> None:
    path = _write(tmp_path / "src" / "edge.py", cfs.MAX_SOURCE_LINES)
    assert cfs.find_oversized([path], tmp_path) == []


def test_oversized_test_module_is_exempt(tmp_path: Path) -> None:
    path = _write(tmp_path / "tests" / "test_huge.py", cfs.MAX_SOURCE_LINES + 500)
    assert cfs.find_oversized([path], tmp_path) == []


def test_oversized_e2e_module_is_exempt(tmp_path: Path) -> None:
    path = _write(tmp_path / "e2e" / "src" / "big.py", cfs.MAX_SOURCE_LINES + 500)
    assert cfs.find_oversized([path], tmp_path) == []
