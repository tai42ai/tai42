"""Command line of the release-train check: exit 0 when the train releases every unit whose ranges it moves.

Run on the checked-out release branch before its ranges are written, with ``--base`` the branch the
release targets. Exit 1 names every uncovered unit and its moves; nothing is written either way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _release_train_check.membership import format_violation, uncovered_moves


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exit 0 when the release branch releases every unit whose ranges or contract pins it moves."
    )
    parser.add_argument("--base", required=True, help="the revision the release targets, e.g. origin/main")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the workspace root (default: cwd)")
    args = parser.parse_args(argv)
    uncovered = uncovered_moves(args.root, args.base)
    if uncovered:
        print(format_violation(args.base, uncovered), file=sys.stderr)
        return 1
    print("release-train: every unit whose ranges or contract pins move is released by this release.")
    return 0
