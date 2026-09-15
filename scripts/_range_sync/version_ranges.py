"""Core version-range derivation and PEP 508 requirement parsing.

The single floor/cap rule and the cross-major guard a pin preserves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# optional environment marker). We only ever rewrite the specifier portion.
_REQ_RE = re.compile(
    r"^\s*"
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*(?P<extras>\[[^\]]*\])?"
    r"\s*(?P<rest>.*)$",
    re.DOTALL,
)

# The integer major of a ``>=<major>...`` floor and a ``<<major>...`` cap. The
# derived range always carries both; a hand-written spec may carry either, both,
# or (for ``~=``/``==``/anything else) neither in this shape.
_FLOOR_RE = re.compile(r">=\s*(\d+)")
_CAP_RE = re.compile(r"<\s*(\d+)")

# A ``~=X.Y`` or ``==X.Y.Z`` spec: both its floor and its cap major are X.
_COMPAT_RE = re.compile(r"[~=]=\s*(\d+)")

# The floor VERSION literal (major[.minor[.patch]]) of a ``>=``/``~=``/``==``
# spec — used to re-derive a descriptor range from a preserved dependency spec.
_FLOOR_VERSION_RE = re.compile(r"(?:>=|~=|==)\s*(\d+(?:\.\d+)*)")


def derive_range(version: str) -> str:
    """Return the derived ``>=floor,<cap`` range for a released *version*.

    Patch level is ignored; the floor is always pinned to MINOR precision
    (``>=major.minor``) — a member claims compatibility only with the sibling
    minor it was built and tested against, so the floor never drops below the
    released minor. Only the cap depends on the 1.0 boundary: pre-1.0 the next
    minor is breaking (cap ``0.<minor+1>``); from 1.0 the next major is
    (cap ``<major+1>``).
    """
    v = version.strip().strip("\"'")
    # Drop any pre-release / local suffix; we only need the numeric release.
    core = re.split(r"[^0-9.]", v, maxsplit=1)[0]
    parts = core.split(".")
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
    # Floor is minor precision regardless of the major (no special-casing);
    # only the cap flips at the 1.0 breaking boundary.
    floor = f"{major}.{minor}"
    cap = f"0.{minor + 1}" if major == 0 else f"{major + 1}"
    return f">={floor},<{cap}"


def _major_structure(spec: str) -> tuple[int | None, int | None]:
    """The ``(floor_major, cap_major)`` integer majors of a specifier; each None when absent or unparseable.

    A ``~=X.Y`` / ``==X.Y.Z`` spec implies floor and cap majors both X; otherwise the floor comes from
    ``>=`` and the cap from ``<``.
    """
    s = spec.strip()
    if not s:
        return (None, None)
    compat = _COMPAT_RE.search(s)
    if compat:
        major = int(compat.group(1))
        return (major, major)
    floor = _FLOOR_RE.search(s)
    cap = _CAP_RE.search(s)
    return (
        int(floor.group(1)) if floor else None,
        int(cap.group(1)) if cap else None,
    )


def is_cross_major(old_spec: str, new_spec: str) -> bool:
    """True when *old_spec* and *new_spec* differ in EITHER the floor's or the cap's integer major.

    A deliberately widened cap (``<3`` derived down to ``<2``) crosses as surely as a raised floor.
    Pre-1.0 both majors are 0, so a 0.x minor bump never crosses. A major that is absent at one end (no
    comparable value) does not, by itself, make that end differ.
    """
    old_floor, old_cap = _major_structure(old_spec)
    new_floor, new_cap = _major_structure(new_spec)
    floor_differs = old_floor is not None and new_floor is not None and old_floor != new_floor
    cap_differs = old_cap is not None and new_cap is not None and old_cap != new_cap
    return floor_differs or cap_differs


def _pin_guarded(old_spec: str, derived: str) -> bool:
    """A rewrite a pin should preserve (and an unpinned cap should warn on).

    The majors cross (floor or cap), or the existing spec's major structure is unparseable — the latter
    treated conservatively as a crossing rather than rewritten blind. An empty spec is version-less and
    never guarded.
    """
    if is_cross_major(old_spec, derived):
        return True
    return bool(old_spec.strip()) and _major_structure(old_spec) == (None, None)


@dataclass(frozen=True)
class ParsedRequirement:
    """A PEP 508 requirement split into the parts range-sync cares about."""

    name: str
    extras: str  # verbatim "[...]" including brackets, or "" if none
    specifier: str  # e.g. ">=0.2,<0.4" (stripped), or "" if version-less
    marker: str  # verbatim ";..." including the leading ';', or "" if none

    def with_specifier(self, new_spec: str) -> str:
        """Rebuild the requirement string with *new_spec* as the specifier.

        Preserves name, extras, and environment marker verbatim.
        """
        return f"{self.name}{self.extras}{new_spec}{self.marker}"


def parse_requirement(raw: str) -> ParsedRequirement | None:
    """Split a requirement string. Returns None if it does not parse."""
    m = _REQ_RE.match(raw)
    if not m:
        return None
    rest = m.group("rest")
    if ";" in rest:
        idx = rest.index(";")
        specifier = rest[:idx].strip()
        marker = rest[idx:]  # keep ';' and everything after, verbatim
    else:
        specifier = rest.strip()
        marker = ""
    return ParsedRequirement(
        name=m.group("name"),
        extras=m.group("extras") or "",
        specifier=specifier,
        marker=marker,
    )
