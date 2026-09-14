"""Fail the lint job when a tracked source module grows past the line limit.

Test modules are exempt: they carry table-driven cases and fixtures that make a
high line count intrinsic rather than a design smell. The e2e tree is exempt for
the same reason -- it is test infrastructure, not shipped source.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

MAX_SOURCE_LINES = 800


def is_test_file(relative_path: Path) -> bool:
    """Whether a repo-relative path is a test module."""
    if any(part == "tests" for part in relative_path.parts):
        return True
    name = relative_path.name
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def is_exempt(relative_path: Path) -> bool:
    """Whether a repo-relative path is exempt from the source line limit.

    Test modules and the whole e2e tree (test infrastructure) are exempt.
    """
    if relative_path.parts and relative_path.parts[0] == "e2e":
        return True
    return is_test_file(relative_path)


def count_lines(path: Path) -> int:
    """Number of physical lines in a file."""
    return len(path.read_text(encoding="utf-8").splitlines())


def find_oversized(files: Iterable[Path], root: Path, limit: int = MAX_SOURCE_LINES) -> list[tuple[Path, int]]:
    """Repo-relative paths (with line counts) of source modules over ``limit``."""
    oversized: list[tuple[Path, int]] = []
    for path in files:
        relative_path = path.relative_to(root)
        if is_exempt(relative_path):
            continue
        line_count = count_lines(path)
        if line_count > limit:
            oversized.append((relative_path, line_count))
    return sorted(oversized)


def tracked_python_files(root: Path) -> list[Path]:
    """Every git-tracked ``.py`` file under ``root``."""
    output = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [root / entry for entry in output.split("\0") if entry]


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parents[1]
    oversized = find_oversized(tracked_python_files(root), root)
    for relative_path, line_count in oversized:
        print(f"{relative_path}: {line_count} lines exceeds the {MAX_SOURCE_LINES}-line limit")
    return 1 if oversized else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
