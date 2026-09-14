"""The ``contract:`` pin in every governed ``tai-plugin.yml``: reading and
rewriting it, discovering the descriptors a member/component ships, and the
per-member contract range a preserved pin dictates."""

from __future__ import annotations

import re
from pathlib import Path

from _range_sync.members import _load_toml, _normalize_name, workspace_globs
from _range_sync.pyproject_specs import analyze_pyproject
from _range_sync.version_ranges import _FLOOR_VERSION_RE, derive_range

# The member whose derived range drives every ``contract:`` pin.
CONTRACT_PACKAGE = "tai42-contract"


# A ``contract:`` line in a tai-plugin.yml, e.g. ``contract: '>=0.3,<0.4'``.
_CONTRACT_RE = re.compile(r"^(?P<indent>\s*)contract:\s*(?P<q>['\"])(?P<val>.*?)(?P=q)(?P<trail>\s*)$")


def rewrite_contract_yaml(text: str, new_range: str) -> tuple[str, bool]:
    """Rewrite the ``contract:`` line's quoted value to *new_range*, keeping the
    quote style. Returns (text, changed)."""
    changed = False
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        newline = ""
        stripped = line
        if line.endswith("\r\n"):
            newline, stripped = "\r\n", line[:-2]
        elif line.endswith("\n"):
            newline, stripped = "\n", line[:-1]
        m = _CONTRACT_RE.match(stripped)
        if m and m.group("val") != new_range:
            q = m.group("q")
            rebuilt = f"{m.group('indent')}contract: {q}{new_range}{q}{m.group('trail')}"
            out_lines.append(rebuilt + newline)
            changed = True
        else:
            out_lines.append(line)
    return "".join(out_lines), changed


def contract_yaml_value(text: str) -> str | None:
    """Return the current ``contract:`` value, or None if absent."""
    for line in text.splitlines():
        m = _CONTRACT_RE.match(line)
        if m:
            return m.group("val")
    return None


def shipped_descriptor_files(member: Path) -> list[Path]:
    """The descriptors a member SHIPS: its root ``tai-plugin.yml`` when it has one,
    plus every copy inside its packaged ``src/`` tree. A ``tai-plugin.yml`` anywhere
    else under the member (a build output, test data) is not shipped and is never
    rewritten."""
    found: list[Path] = []
    root_copy = member / "tai-plugin.yml"
    if root_copy.is_file():
        found.append(root_copy)
    found.extend(sorted((member / "src").rglob("tai-plugin.yml")))
    return found


def plugin_descriptor_files(members: list[Path], root: Path) -> list[tuple[Path, Path]]:
    """Every ``(member, tai-plugin.yml)`` pair (root + packaged copies) under each
    plugin member — the owning member is carried so a member-specific contract
    range (a preserved pin) can override the global one."""
    files: list[tuple[Path, Path]] = []
    for member in members:
        if member.relative_to(root).parts[0] != "plugins":
            continue
        for yml in shipped_descriptor_files(member):
            files.append((member, yml))
    return files


def scaffold_descriptor_files(members: list[Path], root: Path) -> list[Path]:
    """Every ``tai-plugin.yml`` a NON-plugin member ships inside its packaged
    ``src/`` tree: the plugin scaffolds a member carries as package data for a
    plugin author to start from. A scaffold is a descriptor-only plugin spec with
    no pyproject of its own, so it follows the GLOBAL derived contract range like a
    descriptor-only component. A scaffold that declares no ``contract:`` at all
    (nothing to keep current) is left as it is."""
    files: list[Path] = []
    for member in members:
        if member.relative_to(root).parts[0] == "plugins":
            continue
        files.extend(sorted((member / "src").rglob("tai-plugin.yml")))
    return files


def descriptor_only_contract_files(root: Path) -> list[Path]:
    """Every descriptor-only component's root ``tai-plugin.yml``: a workspace-glob
    dir that carries a ``tai-plugin.yml`` but NO ``pyproject.toml`` (the connector
    dirs the root pyproject lists under ``[tool.uv.workspace].exclude`` — they
    ship no package, so ``discover_members`` never sees them). Discovery mirrors
    the packaged split: same globs, partitioned on the presence of a pyproject.
    Each such component carries no pyproject pin to preserve, so its descriptor
    follows the GLOBAL derived contract range exactly like an unpinned member."""
    files: list[Path] = []
    for pattern in workspace_globs(root):
        for hit in sorted(root.glob(pattern)):
            if hit.is_dir() and (hit / "tai-plugin.yml").is_file() and not (hit / "pyproject.toml").is_file():
                files.append(hit / "tai-plugin.yml")
    return sorted(set(files))


def _contract_range(first_party: dict[str, str]) -> str:
    key = _normalize_name(CONTRACT_PACKAGE)
    if key not in first_party:
        raise RuntimeError(f"{CONTRACT_PACKAGE} not found among workspace members")
    return derive_range(first_party[key])


def _descriptor_range_from_spec(spec: str) -> str | None:
    """The contract range a descriptor should advertise given a preserved
    ``tai42-contract`` dependency *spec*: derived from the spec's floor version
    exactly as the global range derives from a released version. None when the
    spec has no parseable floor — the descriptor is then left untouched rather
    than forced to a guessed range."""
    m = _FLOOR_VERSION_RE.search(spec)
    if not m:
        return None
    return derive_range(m.group(1))


def _preserved_contract_ranges(members: list[Path], first_party: dict[str, str], root: Path) -> dict[str, str | None]:
    """Map member-path -> the contract range that member's descriptors must
    advertise, for every member whose ``tai42-contract`` dependency a pin
    preserved. The value is the range derived from the preserved spec's floor;
    it is None when that spec has no derivable floor — an explicit leave-alone
    marker so apply/check skip rewriting that member's descriptor entirely
    rather than forcing it to the global range the pin refuses. (Absence from
    the map, by contrast, means the member is unpinned and follows the global
    range.)"""
    contract_key = _normalize_name(CONTRACT_PACKAGE)
    out: dict[str, str | None] = {}
    for member in members:
        pyproject = _load_toml(member / "pyproject.toml")
        member_path = member.relative_to(root).as_posix()
        analysis = analyze_pyproject(pyproject, first_party, member_path)
        for preserved in analysis.preserved:
            if _normalize_name(preserved.dep_name) != contract_key:
                continue
            out[member_path] = _descriptor_range_from_spec(preserved.kept_range)
    return out
