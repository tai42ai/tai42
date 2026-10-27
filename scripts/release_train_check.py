#!/usr/bin/env python3
"""Release-train check — runnable entrypoint + public facade.

Asserts that a release branch releases every unit whose first-party ranges or contract pins
``range_sync.py`` is about to write over it. The implementation lives in the private
``_release_train_check`` package; this module re-exports its public surface and is the entrypoint
the release workflow invokes by path.
"""

from __future__ import annotations

from _release_train_check.cli import main
from _release_train_check.membership import UncoveredUnit, format_violation, uncovered_moves
from _release_train_check.unit_versions import revision_version, verify_revision, version_file, working_tree_version

__all__ = [
    "UncoveredUnit",
    "format_violation",
    "main",
    "revision_version",
    "uncovered_moves",
    "verify_revision",
    "version_file",
    "working_tree_version",
]


if __name__ == "__main__":
    raise SystemExit(main())
