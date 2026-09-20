"""PEP 440 version + specifier-set validation for a plugin spec.

``VERSION_RE`` matches a single canonical version; ``_check_specifier_clause`` validates one
clause of a ``contract`` compatibility range.
"""

from __future__ import annotations

import re

# PEP 440 version — the spec's canonical pattern, anchored, case-insensitive.
_VERSION_CORE = r"""
    v?
    (?:[0-9]+!)?                                                  # epoch
    [0-9]+(?:\.[0-9]+)*                                           # release
    (?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?    # pre-release
    (?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?                 # post-release
    (?:[-_.]?dev[-_.]?[0-9]*)?                                    # dev release
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?                           # local version
"""
VERSION_RE = re.compile(rf"^{_VERSION_CORE}$", re.IGNORECASE | re.VERBOSE)

# Release-only stem a ``.*`` wildcard clause may carry (``==0.1.*``).
_WILDCARD_STEM_RE = re.compile(r"^v?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*$", re.IGNORECASE)

# ``~=`` (compatible release) needs at least two release segments.
_COMPATIBLE_RELEASE_RE = re.compile(r"^v?(?:[0-9]+!)?[0-9]+\.[0-9]+", re.IGNORECASE)

# Longest operators first so ``===`` is never matched as ``==``.
_SPECIFIER_OPS = ("===", "~=", "==", "!=", "<=", ">=", "<", ">")


def check_specifier_clause(clause: str) -> None:
    """Validate one PEP 440 specifier clause (``>=0.1``, ``==1.2.*``, ...).

    Raises ``ValueError`` naming the clause on any deviation: a missing or
    unknown operator, an unparseable version, a ``.*`` wildcard outside
    ``==``/``!=``, a local version on an ordered comparison, or a ``~=`` stem
    with fewer than two release segments.
    """
    op = next((candidate for candidate in _SPECIFIER_OPS if clause.startswith(candidate)), None)
    if op is None:
        raise ValueError(f"specifier clause {clause!r} does not start with a PEP 440 comparison operator")
    version = clause.removeprefix(op).strip()
    if not version:
        raise ValueError(f"specifier clause {clause!r} names no version")
    if op == "===":
        # Arbitrary equality compares the raw string; any non-empty value is legal.
        return
    if version.endswith(".*"):
        if op not in ("==", "!="):
            raise ValueError(f"specifier clause {clause!r}: a .* wildcard is only legal with == or !=")
        if not _WILDCARD_STEM_RE.fullmatch(version.removesuffix(".*")):
            raise ValueError(f"specifier clause {clause!r}: the wildcard stem must be a plain release segment")
        return
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"specifier clause {clause!r}: {version!r} is not a valid PEP 440 version")
    if op in ("~=", "<", "<=", ">", ">=") and "+" in version:
        raise ValueError(f"specifier clause {clause!r}: a local version is not legal with {op}")
    if op == "~=" and not _COMPATIBLE_RELEASE_RE.match(version):
        raise ValueError(f"specifier clause {clause!r}: ~= needs at least two release segments")
