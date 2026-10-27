"""The release train's membership invariant: every unit whose ranges it writes is a unit it releases.

The moves are the first-party ranges and ``contract:`` pins ``range_sync`` reports as out of sync
over the train's tree — exactly what ``range_sync.py`` is about to write. A unit is released by the
train when the version it declares differs from the one it declares at the base revision. A moved
unit that is not released would ship a tree whose ranges its published version does not carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from _range_sync.members import discover_members
from _range_sync.query import RangeChange, changes_by_unit, release_units
from _range_sync.sync import check
from _release_train_check.unit_versions import revision_version, verify_revision, version_file, working_tree_version


@dataclass(frozen=True)
class UncoveredUnit:
    """A unit whose ranges or contract pins move but which the train does not release.

    ``version_file`` is the repo-relative file the unit's version lives in on the train;
    ``base_version`` is the version that file declares at the base revision — equal to the one the
    train declares — or ``None`` when the base carries no such file.
    """

    version_file: str
    base_version: str | None
    changes: list[RangeChange]


def uncovered_moves(root: Path, base: str) -> dict[str, UncoveredUnit]:
    """Each unit whose ranges or contract pins move over the tree at *root* but which the train does not release.

    A unit is released when its working-tree version differs from its version at *base*; a unit whose
    version file *base* does not carry is not released by this train. Units are listed in path order.
    """
    verify_revision(root, base)
    members = discover_members(root)
    moved = changes_by_unit(check(root), release_units(root, members))
    uncovered: dict[str, UncoveredUnit] = {}
    for unit, changes in moved.items():
        base_version = revision_version(root, base, unit)
        if base_version is None or base_version == working_tree_version(root, unit):
            uncovered[unit] = UncoveredUnit(version_file(root, unit), base_version, changes)
    return uncovered


def _not_released_reason(base: str, entry: UncoveredUnit) -> str:
    if entry.base_version is None:
        return f"{base} carries no {entry.version_file}"
    return f"its version {entry.base_version} equals {base}'s"


def format_violation(base: str, uncovered: dict[str, UncoveredUnit]) -> str:
    """The failure text: every uncovered unit with each of its moves, then what the failure means."""
    lines = [
        f"release-train: {unit} moves {change.file}: {change.old!r} -> {change.new!r} "
        f"but this release does not release {unit} ({_not_released_reason(base, entry)})"
        for unit, entry in uncovered.items()
        for change in entry.changes
    ]
    lines.append(
        "release-train: the release branch was built on a tree its range query did not read, or the query "
        "missed these units; the release-please run for the current main tip rebuilds the release branch."
    )
    return "\n".join(lines)
