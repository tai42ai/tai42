"""Which release units' ranges a candidate set of versions moves.

A release unit is a workspace member (a directory with a ``pyproject.toml``) or a descriptor-only
component (a workspace-glob directory with a ``tai-plugin.yml`` and no ``pyproject.toml``). Given
the versions a release would publish, keyed by unit path, the query derives every first-party range
and ``contract:`` pin under those versions — the on-disk version of every unit not in the candidate
set — and reports each unit whose files would change when ``apply`` writes them. Nothing is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from _range_sync.contract_pins import descriptor_only_contract_files
from _range_sync.members import _load_toml, _normalize_name, discover_members, first_party_versions
from _range_sync.sync import SyncReport, drift_under

# A release version: ``major.minor.patch`` with an optional pre-release / local suffix.
_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.+-]+)?$")


@dataclass(frozen=True)
class RangeChange:
    """One governed value a candidate release moves: in ``file``, ``subject`` goes ``old`` -> ``new``.

    ``subject`` is the first-party dependency name for a ``pyproject.toml`` requirement, or
    ``contract`` for a ``tai-plugin.yml`` pin.
    """

    file: str
    subject: str
    old: str
    new: str


def release_units(root: Path, members: list[Path]) -> list[str]:
    """Every member path and descriptor-only component path, repo-relative."""
    units = [member.relative_to(root).as_posix() for member in members]
    units.extend(yml.parent.relative_to(root).as_posix() for yml in descriptor_only_contract_files(root))
    return sorted(units)


def _owning_unit(file: str, units: list[str]) -> str:
    """The release unit whose directory holds *file*; raises when none does."""
    for unit in units:
        if file.startswith(f"{unit}/"):
            return unit
    raise RuntimeError(f"range-sync: {file} belongs to no workspace member or descriptor-only component")


def _candidate_first_party(
    root: Path, members: list[Path], versions: dict[str, str], units: list[str]
) -> dict[str, str]:
    """The on-disk first-party version map with each candidate member's version put in its place.

    Raises on a path that is no release unit or a value that is no release version. A
    descriptor-only component's version is accepted and changes no range: nothing depends on it.
    """
    unknown = sorted(path for path in versions if path not in units)
    if unknown:
        raise ValueError(f"range-sync query: not a workspace member or descriptor-only component: {unknown}")
    malformed = sorted(
        f"{path}={version!r}" for path, version in versions.items() if not _RELEASE_VERSION_RE.match(version)
    )
    if malformed:
        raise ValueError(f"range-sync query: not a release version: {malformed}")
    first_party = first_party_versions(members)
    for member in members:
        member_path = member.relative_to(root).as_posix()
        if member_path not in versions:
            continue
        name = _load_toml(member / "pyproject.toml").get("project", {}).get("name")
        if not name:
            raise ValueError(f"range-sync query: {member_path}/pyproject.toml declares no [project].name")
        first_party[_normalize_name(str(name))] = versions[member_path]
    return first_party


def changes_by_unit(report: SyncReport, units: list[str]) -> dict[str, list[RangeChange]]:
    """Map each release unit a drift *report* touches to its moves, units in path order."""
    changed: dict[str, list[RangeChange]] = {}
    for member_path, change in report.spec_changes:
        changed.setdefault(member_path, []).append(
            RangeChange(f"{member_path}/pyproject.toml", change.dep_name, change.old_req, change.new_req)
        )
    for yaml_path, old, new in report.contract_changes:
        changed.setdefault(_owning_unit(yaml_path, units), []).append(RangeChange(yaml_path, "contract", old, new))
    return {unit: changed[unit] for unit in sorted(changed)}


def query(root: Path, versions: dict[str, str]) -> dict[str, list[RangeChange]]:
    """Map each release unit whose ranges or contract pins move under *versions* to what moves.

    *versions* maps a unit path (``core/contract``, ``plugins/demo``) to the version a release
    would publish for it. Units are listed in path order; a unit whose governed values all stay
    as they are is absent.
    """
    members = discover_members(root)
    units = release_units(root, members)
    return changes_by_unit(drift_under(root, members, _candidate_first_party(root, members, versions, units)), units)
