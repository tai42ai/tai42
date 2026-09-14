"""Derive first-party ``tai42-*`` version ranges from the released member
versions and rewrite them in place, so ranges never go stale by hand.

Two surfaces are kept in lockstep with the released versions:

  (A) first-party ``tai42-*`` dependency ranges in every workspace member's
      ``pyproject.toml`` (``[project].dependencies`` +
      ``[project.optional-dependencies]``);
  (B) the ``contract:`` pin in every ``tai-plugin.yml`` (both the root copy and
      the packaged ``src/.../tai-plugin.yml`` copy of each plugin, plus the root
      copy of every DESCRIPTOR-ONLY component — a workspace-glob dir carrying a
      ``tai-plugin.yml`` but no ``pyproject.toml``, so it ships no package and is
      never a member — plus every SCAFFOLD descriptor a non-plugin member ships
      inside its own ``src/`` tree), which tracks ``tai42-contract``. A
      descriptor-only component carries no pyproject pin to preserve, so its
      descriptor follows the GLOBAL derived contract range exactly like an
      unpinned member's; a scaffold describes a descriptor-only plugin its author
      will publish, so it follows the global range too.

The version source of truth is each member's ``[project].version``. The range
for a released version ``V`` is derived by a single rule (patch is ignored):

  * floor = ``<major>.<minor>`` always (minor precision; the released minor)
  * cap   = ``0.<minor+1>`` pre-1.0 (major == 0), else ``<major+1>`` from 1.0

e.g. 0.3.0 -> ``>=0.3,<0.4``; 0.5.1 -> ``>=0.5,<0.6``; 1.2.3 -> ``>=1.2,<2``.

Files are edited by targeted string/line replacement (not a toml/yaml
round-trip) so formatting and comments are preserved. Reading uses ``tomllib``.
Pure Python standard library only; no clock, network, or randomness.

A dependant may mark a first-party cap as deliberate with a
``[tool.range-sync] pinned = ["<dep-name>", ...]`` table in its own
``pyproject.toml``. A pinned dep whose derived range would cross a MAJOR at
either end — the floor major or the cap major, so a deliberately WIDENED cap
(``>=1.2,<3``) counts as much as a raised floor, and a ``~=``/``==`` spec whose
implied majors move counts too — is left untouched (and reported) instead of
rewritten; a pinned dep whose existing spec has an unparseable major structure
is likewise preserved conservatively rather than rewritten blind. Minor/patch
syncs within one major ignore the pin and behave identically for every dep. The
pin decision is read from the same ``tomllib`` parse that discovers
requirements, so it is robust regardless of the text-level rewrite path (a line
comment would be invisible to that parse). An unpinned dep whose rewrite is
guarded (crosses at either end, or unparseable) still syncs, but ``--check``
emits a non-failing WARNING so a deliberate cap can be annotated. When a pin
preserves a member's ``tai42-contract`` range, that member's ``contract:``
descriptor follows the preserved dep (derived from its floor) rather than the
global released range; when the preserved spec has no derivable floor the
descriptor is left untouched entirely (and reported) rather than forced to the
global range — so a pinned member never advertises a contract major its
dependency refuses. A ``pinned`` name that is not a first-party dependency of
that member is a loud error, and a ``[tool.range-sync]`` table on a non-member
pyproject (e.g. the root) is a loud error too — it would silently do nothing.

CLI:
  python scripts/range_sync.py            # APPLY: rewrite in place (default)
  python scripts/range_sync.py --check    # verify sync; exit 1 + diff on drift
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _range_sync.pyproject_specs import PIN_KEY, PIN_TABLE
from _range_sync.sync import SyncReport, _format_drift, apply, check


def _repo_root() -> Path:
    """The repo root is the parent of this script's ``scripts/`` directory."""
    return Path(__file__).resolve().parents[2]


def _print_preserved(report: SyncReport) -> None:
    for member_path, preserved in report.preserved:
        print(f"range-sync: {member_path}/pyproject.toml: {preserved.dep_name} pinned, left at {preserved.kept_range}")


def _print_descriptor_untouched(report: SyncReport) -> None:
    for yaml_path, kept in report.descriptor_untouched:
        print(f"range-sync: {yaml_path}: descriptor left untouched (pinned, underivable floor), at {kept!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify ranges are already synced; exit 1 with a diff on drift",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repo root (defaults to the parent of scripts/)",
    )
    args = parser.parse_args(argv)
    root = (args.root or _repo_root()).resolve()

    if args.check:
        report = check(root)
        _print_preserved(report)
        _print_descriptor_untouched(report)
        for member_path, change in report.warnings:
            print(
                f"range-sync: WARNING: {member_path}/pyproject.toml: cross-major rewrite of an "
                f"unannotated cap for {change.dep_name}: {change.old_req!r} -> {change.new_req!r} — "
                f"annotate the cap in [tool.{PIN_TABLE}].{PIN_KEY} if it is deliberate."
            )
        if report.dirty:
            print("range-sync: OUT OF SYNC — derived ranges do not match the formula:")
            print(_format_drift(report))
            print("\nRun `python scripts/range_sync.py` to fix.")
            return 1
        print("range-sync: all first-party ranges and contract pins are in sync.")
        return 0

    report = apply(root)
    _print_preserved(report)
    _print_descriptor_untouched(report)
    if report.dirty:
        print("range-sync: rewrote the derived ranges:")
        print(_format_drift(report))
    else:
        print("range-sync: nothing to change; already in sync.")
    return 0
