"""Rewrite (or verify) every member's first-party ranges and each governed descriptor's ``contract:`` pin.

Apply mode rewrites, check mode verifies, both against the derived formula.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from _range_sync.contract_pins import (
    _contract_range,
    _preserved_contract_ranges,
    contract_yaml_value,
    descriptor_only_contract_files,
    plugin_descriptor_files,
    rewrite_contract_yaml,
    scaffold_descriptor_files,
)
from _range_sync.members import _load_toml, discover_members, first_party_versions
from _range_sync.pyproject_specs import (
    PIN_TABLE,
    Preserved,
    PyprojectAnalysis,
    SpecChange,
    analyze_pyproject,
    rewrite_pyproject_text,
)


@dataclass
class SyncReport:
    """What an apply run changed / what a check run found out of sync.

    ``preserved``, ``warnings`` and ``descriptor_untouched`` are informational
    only: a preserved pin is not a drift, a warning does not fail the gate, and a
    descriptor left untouched under an underivable-floor pin is not a rewrite — so
    none of them feed ``dirty``.
    """

    spec_changes: list[tuple[str, SpecChange]]  # (member_path, change)
    contract_changes: list[tuple[str, str, str]]  # (yaml_path, old, new)
    preserved: list[tuple[str, Preserved]]  # (member_path, preserved)
    warnings: list[tuple[str, SpecChange]]  # (member_path, unpinned cross-major change)
    descriptor_untouched: list[tuple[str, str]]  # (yaml_path, left-at contract value)

    @property
    def dirty(self) -> bool:
        return bool(self.spec_changes or self.contract_changes)


def _assert_no_stray_pin_tables(root: Path, members: list[Path]) -> None:
    """A ``[tool.range-sync]`` table is only honoured on a workspace MEMBER pyproject.

    The same table on any non-member pyproject the script reads (the root) would silently do nothing —
    raise so it is never mistaken for an active pin.
    """
    member_dirs = {m.resolve() for m in members}
    stray: list[str] = []
    root_py = root / "pyproject.toml"
    if root_py.is_file() and root.resolve() not in member_dirs and PIN_TABLE in _load_toml(root_py).get("tool", {}):
        stray.append(root_py.relative_to(root).as_posix())
    if stray:
        raise RuntimeError(f"pin tables belong on member pyprojects: {stray}")


def _empty_report() -> SyncReport:
    return SyncReport(
        spec_changes=[],
        contract_changes=[],
        preserved=[],
        warnings=[],
        descriptor_untouched=[],
    )


def _collect_pyproject_analysis(
    members: list[Path], first_party: dict[str, str], root: Path
) -> list[tuple[str, Path, str, PyprojectAnalysis]]:
    """Analyse every member pyproject once — the shared read+analyse apply and check both drive.

    Returns ``(member_path, py_path, text, analysis)`` per member.
    """
    analyses: list[tuple[str, Path, str, PyprojectAnalysis]] = []
    for member in members:
        py_path = member / "pyproject.toml"
        text = py_path.read_text()
        pyproject = tomllib.loads(text)
        member_path = member.relative_to(root).as_posix()
        analyses.append((member_path, py_path, text, analyze_pyproject(pyproject, first_party, member_path)))
    return analyses


def _resolve_descriptor_target(
    member_path: str,
    yaml_path: str,
    current: str | None,
    preserved_contract: dict[str, str | None],
    contract_range: str,
    report: SyncReport,
) -> str | None:
    """The contract range a plugin descriptor must advertise — the shared decision apply and check both make.

    A member whose ``tai42-contract`` pin was preserved follows the range derived from that pin; when the
    pin has no derivable floor the descriptor is left untouched (recorded, and ``None`` returned so the
    caller skips it). An unpinned member follows the global derived range.
    """
    if member_path in preserved_contract:
        target = preserved_contract[member_path]
        if target is None:
            report.descriptor_untouched.append((yaml_path, current or ""))
            return None
        return target
    return contract_range


def _apply_pyproject_writes(
    analyses: list[tuple[str, Path, str, PyprojectAnalysis]], report: SyncReport
) -> list[tuple[Path, str]]:
    """Record each member's preserved/warning/change rows and queue rewritten pyproject text where changed."""
    writes: list[tuple[Path, str]] = []
    for member_path, py_path, text, analysis in analyses:
        for preserved in analysis.preserved:
            report.preserved.append((member_path, preserved))
        for warning in analysis.warnings:
            report.warnings.append((member_path, warning))
        if analysis.changes:
            new_text, _ = rewrite_pyproject_text(text, analysis.changes)
            writes.append((py_path, new_text))
            for change in analysis.changes:
                report.spec_changes.append((member_path, change))
    return writes


def _apply_descriptor_writes(
    members: list[Path],
    root: Path,
    preserved_contract: dict[str, str | None],
    contract_range: str,
    report: SyncReport,
) -> list[tuple[Path, str]]:
    """Queue the rewritten text for every governed descriptor whose ``contract:`` pin changes.

    The plugin descriptors (honouring a preserved per-member range) and the descriptor-only + scaffold
    descriptors (always the global range).
    """
    writes: list[tuple[Path, str]] = []
    for member, yml in plugin_descriptor_files(members, root):
        member_path = member.relative_to(root).as_posix()
        yaml_path = yml.relative_to(root).as_posix()
        text = yml.read_text()
        old = contract_yaml_value(text)
        target = _resolve_descriptor_target(member_path, yaml_path, old, preserved_contract, contract_range, report)
        if target is None:
            continue
        new_text, changed = rewrite_contract_yaml(text, target)
        if changed:
            writes.append((yml, new_text))
            report.contract_changes.append((yaml_path, old or "", target))
    for yml in (*descriptor_only_contract_files(root), *scaffold_descriptor_files(members, root)):
        yaml_path = yml.relative_to(root).as_posix()
        text = yml.read_text()
        old = contract_yaml_value(text)
        new_text, changed = rewrite_contract_yaml(text, contract_range)
        if changed:
            writes.append((yml, new_text))
            report.contract_changes.append((yaml_path, old or "", contract_range))
    return writes


def apply(root: Path) -> SyncReport:
    """Rewrite every member pyproject + every governed descriptor in place.

    Every surface is derived and validated BEFORE any file is written, so a refused surface leaves the tree
    exactly as it was. Idempotent.
    """
    members = discover_members(root)
    _assert_no_stray_pin_tables(root, members)
    first_party = first_party_versions(members)
    contract_range = _contract_range(first_party)
    preserved_contract = _preserved_contract_ranges(members, first_party, root)
    report = _empty_report()

    analyses = _collect_pyproject_analysis(members, first_party, root)
    writes = _apply_pyproject_writes(analyses, report)
    writes.extend(_apply_descriptor_writes(members, root, preserved_contract, contract_range, report))

    for path, new_text in writes:
        path.write_text(new_text)

    _self_assert(root)
    return report


def _self_assert(root: Path) -> None:
    """After applying, re-derive and confirm every rewritten specifier and contract pin equals the formula.

    Raises on any mismatch, naming every surface still out of sync.
    """
    drift = check(root)
    if drift.dirty:
        raise RuntimeError(f"self-assert failed after apply:\n{_format_drift(drift)}")


def _check_descriptors(
    members: list[Path],
    root: Path,
    preserved_contract: dict[str, str | None],
    contract_range: str,
    report: SyncReport,
) -> None:
    """Record every governed descriptor whose ``contract:`` pin is out of sync with its required range.

    Never modifies any file.
    """
    for member, yml in plugin_descriptor_files(members, root):
        member_path = member.relative_to(root).as_posix()
        yaml_path = yml.relative_to(root).as_posix()
        current = contract_yaml_value(yml.read_text())
        target = _resolve_descriptor_target(member_path, yaml_path, current, preserved_contract, contract_range, report)
        if target is None:
            continue
        if current is not None and current != target:
            report.contract_changes.append((yaml_path, current, target))
    for yml in (*descriptor_only_contract_files(root), *scaffold_descriptor_files(members, root)):
        yaml_path = yml.relative_to(root).as_posix()
        current = contract_yaml_value(yml.read_text())
        if current is not None and current != contract_range:
            report.contract_changes.append((yaml_path, current, contract_range))


def check(root: Path) -> SyncReport:
    """Verify every first-party specifier and contract pin already equals the formula output.

    Returns a report of any drift (does not modify files).
    """
    members = discover_members(root)
    _assert_no_stray_pin_tables(root, members)
    first_party = first_party_versions(members)
    contract_range = _contract_range(first_party)
    preserved_contract = _preserved_contract_ranges(members, first_party, root)
    report = _empty_report()

    for member_path, _py_path, _text, analysis in _collect_pyproject_analysis(members, first_party, root):
        for change in analysis.changes:
            report.spec_changes.append((member_path, change))
        for preserved in analysis.preserved:
            report.preserved.append((member_path, preserved))
        for warning in analysis.warnings:
            report.warnings.append((member_path, warning))

    _check_descriptors(members, root, preserved_contract, contract_range, report)
    return report


def _format_drift(report: SyncReport) -> str:
    lines: list[str] = []
    for member_path, change in report.spec_changes:
        lines.append(f"  {member_path}/pyproject.toml: {change.old_req!r} -> {change.new_req!r}")
    for yaml_path, old, new in report.contract_changes:
        lines.append(f"  {yaml_path}: contract {old!r} -> {new!r}")
    return "\n".join(lines)
