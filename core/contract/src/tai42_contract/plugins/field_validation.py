"""Reusable manifest scalar-field shape validators shared by the item and spec models.

The regex constants for a listing's slug/package/tag/name/module/license/icon/migrations
shapes, the display-name cap, and the pure control-char / one-line / tag checks that guard
them. A cohesive set of field-shape rules, not a dumping ground.
"""

from __future__ import annotations

import re

from tai42_contract._urls import is_control_char

# Lowercase slug for the publisher namespace, the listing name, and categories:
# a letter, then lowercase alphanumerics/hyphens.
LISTING_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Normalized pip distribution name (lowercase alphanumeric runs joined by
# single hyphens) — the spec stores the normalized form only, so lookups never
# need PEP 503 re-normalization.
PACKAGE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Free-form tag: lowercase alphanumeric start, then alphanumerics, hyphens,
# or underscores.
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Registered item name (a tool/extension/... registration identifier).
ITEM_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Dotted Python import path: identifier segments joined by dots.
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

# SPDX license-id shape (e.g. ``Apache-2.0``, ``BSD-3-Clause``, ``GPL-3.0+``).
# Whether the id exists in the SPDX list is a registry data concern, not a
# schema concern.
LICENSE_RE = re.compile(r"^[A-Za-z0-9.+-]+$")

# A packaged icon path: a relative POSIX path whose segments are filename-safe
# characters joined by single forward slashes — no leading ``/`` (absolute), no
# backslash or drive, and (checked in the validator) no ``..`` segment. The
# alternative icon form is an ``https://`` URL, matched separately.
ICON_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

# A packaged migrations directory: a package-relative POSIX path whose segments
# are filename-safe characters joined by single forward slashes — no leading
# ``/`` (absolute), no trailing slash, no backslash or drive, and (checked in
# the validator) no ``..`` segment. The contract validates only this shape;
# whether the directory exists in the installed package is a runner-discovery
# concern (``importlib.resources``), never a contract concern — ``PluginSpec``
# is also hydrated from stored DB rows and can touch no filesystem.
MIGRATIONS_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

# Longest single-line UI title accepted for ``display_name`` — a listing card's
# title stays bounded.
DISPLAY_NAME_MAX_LEN = 80


def has_disallowed_control_char(value: str) -> bool:
    """True if ``value`` holds any C0 or C1 control character, ``DEL``, a Unicode
    line/paragraph separator, or a bidirectional / zero-width format control
    (Trojan-Source spoofing) — the class that enables terminal-escape /
    line-overwrite / visual-spoofing injection. A regular ASCII space
    (``0x20``), U+200D ZWJ, and U+200C ZWNJ are allowed, so ordinary spaced
    prose, emoji sequences, and legitimate Persian/Farsi text pass."""
    return any(is_control_char(ch) for ch in value)


def check_one_line(value: str, *, field: str = "description") -> str:
    if not value.strip():
        raise ValueError(f"{field} must be non-empty")
    if has_disallowed_control_char(value):
        raise ValueError(f"{field} must be a single line with no control characters")
    return value


def check_tags(value: list[str]) -> list[str]:
    if len(value) > 10:
        raise ValueError(f"at most 10 tags are allowed, got {len(value)}")
    if len(set(value)) != len(value):
        raise ValueError("tags must be unique")
    for tag in value:
        if not TAG_RE.fullmatch(tag):
            raise ValueError(f"tag {tag!r} must match {TAG_RE.pattern}")
    return value
