"""Plugin manifest contract: the ``tai-plugin.yml`` schema (``PluginSpec``).

Every installable TAI plugin ships a ``tai-plugin.yml`` — at its repo root and
as package-data inside the built wheel. The file names the listing
(``namespace/name``), the pip distribution that backs it, the tai42-contract
compatibility range, declared capabilities, and the item-level ``provides``
index: one entry per tool/agent/extension/... the package registers, because
items are what users search for while the plugin is what gets installed.

The models here are the one schema shared by the marketplace registry's
validator, the skeleton's installer, and each plugin repo's own spec test.
The YAML I/O helpers live above the contract (``tai42_kit.plugins``) — the
contract itself has no YAML dependency.

``KIND_MANIFEST_BINDINGS`` is the single source for how each provided item
kind wires into a :class:`~tai42_contract.manifest.Manifest`: which manifest
field an installer patches and with what shape (a config row, a module-list
entry, a single-module slot, a package-name entry, an ``mcp`` transport entry,
or no manifest field at all for the env-selected ``config`` kind).

Version strings are validated as PEP 440 (an anchored regex of the spec's
canonical pattern) and ``contract`` as a PEP 440 specifier set — shape-level
parseability only; evaluating whether a version satisfies a range is the
consumer's concern.

``display_name`` and ``icon`` are the optional marketplace display metadata: a
human UI title (the UI titleizes ``name`` when absent) and either a packaged
image path relative to the package root or an ``https`` URL (the UI falls back
to a generated monogram when absent).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.manifest import MCPConfig

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

# One literal segment of a declared route path: filename-safe characters, no
# slash. A template segment (``{name}``) is matched separately.
ROUTE_LITERAL_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# One template segment of a declared route path: ``{name}`` where ``name`` is a
# python identifier. A Starlette converter suffix (``{x:path}``) is INVALID —
# plugin declarations never carry converters.
ROUTE_PARAM_SEGMENT_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")

# One segment of a route mount base: lowercase alphanumerics and interior
# hyphens, no leading/trailing hyphen. Segments join with ``/``; the base itself
# is relative (no leading/trailing slash) and carries no templates.
ROUTE_BASE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# Longest single-line UI title accepted for ``display_name`` — a listing card's
# title stays bounded.
DISPLAY_NAME_MAX_LEN = 80

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


def _check_specifier_clause(clause: str) -> None:
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


# Unicode line/paragraph separators that a naive ``"\n" in value`` check misses
# but which drive a line break (or overwrite) in a terminal or UI. (U+0085 NEL
# is itself a C1 control and is caught by the 0x7F..0x9F range below.)
_UNICODE_LINE_SEPARATORS = frozenset("\u2028\u2029\u0085")


# Unicode bidirectional and zero-width format controls. In free text rendered
# untrusted in a marketplace UI these enable Trojan-Source visual spoofing
# (CVE-2021-42574): bidi overrides/isolates reorder displayed characters and
# zero-width spaces/BOM hide or splice tokens. Held as explicit code points (not
# a Unicode-category ban) so that U+200D ZWJ (emoji sequences) and U+200C ZWNJ
# (required for legitimate Persian/Farsi text) — and all ordinary RTL letters —
# stay allowed.
_BIDI_FORMAT_CONTROLS = frozenset(
    {
        "؜",  # ARABIC LETTER MARK
        "‎",  # LEFT-TO-RIGHT MARK
        "‏",  # RIGHT-TO-LEFT MARK
        "‪",  # LEFT-TO-RIGHT EMBEDDING
        "‫",  # RIGHT-TO-LEFT EMBEDDING
        "‬",  # POP DIRECTIONAL FORMATTING
        "‭",  # LEFT-TO-RIGHT OVERRIDE
        "‮",  # RIGHT-TO-LEFT OVERRIDE
        "⁦",  # LEFT-TO-RIGHT ISOLATE
        "⁧",  # RIGHT-TO-LEFT ISOLATE
        "⁨",  # FIRST STRONG ISOLATE
        "⁩",  # POP DIRECTIONAL ISOLATE
        "​",  # ZERO WIDTH SPACE
        "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
    }
)


def _is_control_char(ch: str) -> bool:
    """True if ``ch`` is a control, line/paragraph separator, or bidirectional /
    zero-width format character: a C0 control (``ord < 0x20``), ``DEL`` or any C1
    control (``0x7F..0x9F``, which covers U+0085 NEL, U+009B CSI, U+009D OSC,
    ...), U+2028/U+2029, or one of the bidi/zero-width format controls in
    :data:`_BIDI_FORMAT_CONTROLS` (LRM/RLM, embeddings/overrides, isolates, ALM,
    ZWSP, BOM). This is the terminal-escape / line-overwrite / Trojan-Source
    injection class. A regular ASCII space (``0x20``), U+200D ZWJ, and U+200C
    ZWNJ are NOT rejected."""
    return ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in _UNICODE_LINE_SEPARATORS or ch in _BIDI_FORMAT_CONTROLS


def _has_disallowed_control_char(value: str) -> bool:
    """True if ``value`` holds any C0 or C1 control character, ``DEL``, a Unicode
    line/paragraph separator, or a bidirectional / zero-width format control
    (Trojan-Source spoofing) — the class that enables terminal-escape /
    line-overwrite / visual-spoofing injection. A regular ASCII space
    (``0x20``), U+200D ZWJ, and U+200C ZWNJ are allowed, so ordinary spaced
    prose, emoji sequences, and legitimate Persian/Farsi text pass."""
    return any(_is_control_char(ch) for ch in value)


def _check_one_line(value: str, *, field: str = "description") -> str:
    if not value.strip():
        raise ValueError(f"{field} must be non-empty")
    if _has_disallowed_control_char(value):
        raise ValueError(f"{field} must be a single line with no control characters")
    return value


def _check_web_url(value: str, *, field: str, schemes: tuple[str, ...]) -> str:
    """Validate a single-line http(s) URL.

    Rejects any whitespace or control character (a URL never contains raw
    whitespace, so this also rules out embedded newlines) and requires a
    parseable ``scheme://host`` whose scheme is in ``schemes``, whose host is
    non-empty, and which carries no embedded userinfo (a ``user@host``
    authority-spoofing form). Raises ``ValueError`` naming ``field``.
    """
    if any(ch.isspace() or _is_control_char(ch) for ch in value):
        raise ValueError(f"{field} must be a single-line URL with no whitespace or control characters")
    try:
        split = urlsplit(value)
    except ValueError:
        raise ValueError(f"{field} must be a {' or '.join(schemes)} URL") from None
    if split.scheme not in schemes:
        raise ValueError(f"{field} must be a {' or '.join(schemes)} URL")
    # A real host and no embedded userinfo: a bare ``scheme://`` (no host), a
    # ``scheme://user@host`` credential form (the ``trusted.com@evil.com``
    # authority-spoofing vector), or a hostless ``scheme://user@`` is rejected.
    if not split.hostname or "@" in split.netloc:
        raise ValueError(f"{field} must include a host and no embedded credentials")
    return value


def _check_tags(value: list[str]) -> list[str]:
    if len(value) > 10:
        raise ValueError(f"at most 10 tags are allowed, got {len(value)}")
    if len(set(value)) != len(value):
        raise ValueError("tags must be unique")
    for tag in value:
        if not TAG_RE.fullmatch(tag):
            raise ValueError(f"tag {tag!r} must match {TAG_RE.pattern}")
    return value


class PluginItemKind(StrEnum):
    """Kind of one installable item a plugin provides.

    The values are the ecosystem's item-kind vocabulary (the same words the
    catalog and the marketplace facets use). ``KIND_MANIFEST_BINDINGS`` maps
    every member onto its manifest wiring; an unknown kind in a spec is a
    loud validation reject, never a skipped row.
    """

    TOOL = "tool"
    AGENT = "agent"
    EXTENSION = "extension"
    CONNECTOR = "connector"
    CHANNEL = "channel"
    BACKEND = "backend"
    STORAGE = "storage"
    MONITORING = "monitoring"
    WEBHOOK_VERIFIER = "webhook-verifier"
    CONFIG = "config"
    IDENTITY = "identity"
    STUDIO_PLUGIN = "studio-plugin"
    ROUTER = "router"
    MIDDLEWARE = "middleware"
    MCP_SERVER = "mcp-server"


class ManifestBinding(BaseModel):
    """How one provided item kind wires into a ``Manifest``.

    ``field`` names the manifest field an installer patches for an item of
    this kind; ``mode`` is the patch shape:

    - ``config_row`` — append a config row (``tools``/``agents``) whose
      ``module`` is the item's module.
    - ``module_list`` — append the item's module to a plain module list.
    - ``scalar_module`` — set a single-module slot; the slot holds ONE module,
      so a second plugin claiming an occupied slot is a conflict the caller
      must reject loudly.
    - ``package_list`` — append the plugin's DISTRIBUTION name (not the item's
      module) to a package list (``studio_plugins``).
    - ``mcp_entry`` — append one ``{title: item.name, config: item.mcp}`` object
      to the manifest ``mcp`` list; uninstall removes the entry by title.
    - ``env_selected`` — no manifest field: the kind is selected through the
      environment (a ``config`` provider is named by ``TAI_CONFIG_MODE`` and
      imported by the config seam), so ``field`` is ``None``.
    """

    model_config = ConfigDict(frozen=True)

    field: str | None
    mode: Literal["config_row", "module_list", "scalar_module", "package_list", "mcp_entry", "env_selected"]

    @model_validator(mode="after")
    def _field_iff_manifest_wired(self) -> ManifestBinding:
        if (self.field is None) != (self.mode == "env_selected"):
            raise ValueError("field must be None exactly when mode is 'env_selected'")
        return self


# The single source of kind→manifest wiring, consumed by the skeleton
# installer (patch/unpatch) and the marketplace registry (item classification).
KIND_MANIFEST_BINDINGS: Mapping[PluginItemKind, ManifestBinding] = MappingProxyType(
    {
        PluginItemKind.TOOL: ManifestBinding(field="tools", mode="config_row"),
        PluginItemKind.AGENT: ManifestBinding(field="agents", mode="config_row"),
        PluginItemKind.EXTENSION: ManifestBinding(field="extensions_modules", mode="module_list"),
        PluginItemKind.CONNECTOR: ManifestBinding(field="lifecycle_modules", mode="module_list"),
        PluginItemKind.CHANNEL: ManifestBinding(field="channel_modules", mode="module_list"),
        PluginItemKind.BACKEND: ManifestBinding(field="backend_module", mode="scalar_module"),
        PluginItemKind.STORAGE: ManifestBinding(field="storage_module", mode="scalar_module"),
        PluginItemKind.MONITORING: ManifestBinding(field="monitoring_module", mode="scalar_module"),
        PluginItemKind.WEBHOOK_VERIFIER: ManifestBinding(field="webhook_verifier_modules", mode="module_list"),
        PluginItemKind.CONFIG: ManifestBinding(field=None, mode="env_selected"),
        PluginItemKind.IDENTITY: ManifestBinding(field="lifecycle_modules", mode="module_list"),
        PluginItemKind.STUDIO_PLUGIN: ManifestBinding(field="studio_plugins", mode="package_list"),
        PluginItemKind.ROUTER: ManifestBinding(field="routers_modules", mode="module_list"),
        PluginItemKind.MIDDLEWARE: ManifestBinding(field="middlewares_modules", mode="module_list"),
        PluginItemKind.MCP_SERVER: ManifestBinding(field="mcp", mode="mcp_entry"),
    }
)


class PluginPermissions(BaseModel):
    """Capabilities a plugin declares — informational: surfaced in listings,
    not enforced by a sandbox.

    Omitting the block declares none (every flag defaults to ``False``); an
    unknown key is rejected loudly rather than silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    network: bool = False
    subprocess: bool = False
    filesystem: bool = False


RouteMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class RouteDecl(BaseModel):
    """One HTTP route a plugin item declares, relative to its mount base.

    ``path`` is ``/``-prefixed with at least one segment; each segment is a
    literal (:data:`ROUTE_LITERAL_SEGMENT_RE`) or a ``{name}`` template
    (:data:`ROUTE_PARAM_SEGMENT_RE`, a python identifier with no converter
    suffix). ``methods`` is a non-empty set of uppercase HTTP methods, unique
    within the row. ``public`` states — with no default, so the declaration is
    never silent — whether the resolved route answers unauthenticated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    methods: list[RouteMethod]
    public: bool

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"route path {value!r} must be '/'-prefixed")
        segments = value.split("/")[1:]
        if not segments or "" in segments:
            raise ValueError(f"route path {value!r} must have >=1 non-empty segment and no trailing slash")
        for segment in segments:
            if segment in (".", ".."):
                raise ValueError(
                    f"route path segment {segment!r} is a path-traversal token, never a valid route segment"
                )
            if "{" in segment or "}" in segment:
                if not ROUTE_PARAM_SEGMENT_RE.fullmatch(segment):
                    raise ValueError(
                        f"route path segment {segment!r} must be a '{{name}}' template with a "
                        "python-identifier name and no converter suffix"
                    )
            elif not ROUTE_LITERAL_SEGMENT_RE.fullmatch(segment):
                raise ValueError(
                    f"route path segment {segment!r} must be a literal ([a-zA-Z0-9._-]+) or a '{{name}}' template"
                )
        return value

    @field_validator("methods")
    @classmethod
    def _check_methods(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("methods must name at least one HTTP method")
        if len(set(value)) != len(value):
            raise ValueError("methods must be unique")
        return value


class RoutesDecl(BaseModel):
    """The route block one item declares: a default mount ``base`` and its rows.

    ``base`` is RELATIVE — one or more :data:`ROUTE_BASE_SEGMENT_RE` segments
    joined by ``/``, no leading/trailing slash, no templates. The resolved
    absolute path of each row is ``/api/`` + ``base`` + the row's ``path``; the
    ``/api/`` root is fixed platform-wide and only ``base`` is remappable.
    ``paths`` is non-empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base: str
    paths: list[RouteDecl]

    @field_validator("base")
    @classmethod
    def _check_base(cls, value: str) -> str:
        if value.startswith("/") or value.endswith("/"):
            raise ValueError(f"route base {value!r} must be relative (no leading or trailing '/')")
        segments = value.split("/")
        for segment in segments:
            if not ROUTE_BASE_SEGMENT_RE.fullmatch(segment):
                raise ValueError(
                    f"route base segment {segment!r} must match {ROUTE_BASE_SEGMENT_RE.pattern} (no templates)"
                )
        return value

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[RouteDecl]) -> list[RouteDecl]:
        if not value:
            raise ValueError("routes must declare at least one path")
        return value


def _route_shape(base: str, path: str) -> tuple[str | None, ...]:
    """Resolved segment shape of one declared route for overlap comparison: the
    ``base`` segments followed by the ``path`` segments, each a literal text or
    ``None`` for a ``{name}`` template position. The fixed ``/api/`` root is a
    constant prefix on every route and omitted."""
    segments: list[str | None] = list(base.split("/"))
    segments.extend(None if seg.startswith("{") else seg for seg in path.split("/")[1:])
    return tuple(segments)


def _shapes_overlap(a: tuple[str | None, ...], b: tuple[str | None, ...]) -> bool:
    """True when two resolved shapes can match one concrete request path: equal
    segment count and, at every position, either side is a template or the two
    literals are equal (a concrete path instantiating a template IS an overlap)."""
    if len(a) != len(b):
        return False
    return all(sa is None or sb is None or sa == sb for sa, sb in zip(a, b, strict=True))


class PluginItem(BaseModel):
    """One installable item in a plugin's ``provides`` index.

    ``module`` is the import path whose import side-effect registers the item
    (or, for env-selected kinds, the module the selecting seam imports); the
    installer patches it into the manifest per ``KIND_MANIFEST_BINDINGS``.

    The item shape is kind-conditional (a model validator enforces it, loud
    both ways): an ``mcp-server`` item carries ``mcp`` transport config and no
    ``module``; every other kind carries ``module`` and no ``mcp``.

    ``routes`` declares the item's HTTP mount: REQUIRED for ``kind: router``,
    OPTIONAL for ``kind: channel``, and FORBIDDEN (must be absent) for every
    other kind.

    ``group`` is an OPTIONAL logical family label the author may put on any item
    of any kind: items sharing a value belong to one family, and a consumer may
    summarize the ``provides`` index by group. It is purely a structural
    self-description of the plugin — cross-kind groups and single-member groups
    are both legal, and absent means the item stands alone. It carries a tag's
    charset discipline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PluginItemKind
    name: str
    module: str | None = None
    mcp: MCPConfig | None = None
    description: str
    tags: list[str] = Field(default_factory=list)
    group: str | None = None
    routes: RoutesDecl | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not ITEM_NAME_RE.fullmatch(value):
            raise ValueError(f"item name {value!r} must match {ITEM_NAME_RE.pattern}")
        return value

    @field_validator("module")
    @classmethod
    def _check_module(cls, value: str | None) -> str | None:
        # Presence is the model validator's concern (required iff not
        # mcp-server); this checks only shape when a value is given.
        if value is None:
            return None
        if not MODULE_RE.fullmatch(value):
            raise ValueError(f"module {value!r} must be a dotted Python import path")
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        return _check_one_line(value)

    @field_validator("tags")
    @classmethod
    def _check_item_tags(cls, value: list[str]) -> list[str]:
        return _check_tags(value)

    @field_validator("group")
    @classmethod
    def _check_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not TAG_RE.fullmatch(value):
            raise ValueError(f"group {value!r} must match {TAG_RE.pattern}")
        return value

    @model_validator(mode="after")
    def _shape_by_kind(self) -> PluginItem:
        if self.kind is PluginItemKind.MCP_SERVER:
            if self.mcp is None:
                raise ValueError(f"kind {self.kind.value!r} requires 'mcp' transport config")
            # A static mcp-server spec must name a reachable transport: MCPConfig
            # permits zero-transport only for runtime entries built empty then
            # mutated, so require it loudly at the plugin boundary rather than
            # letting an empty shell surface late at spawn. Its own validator
            # already guarantees at MOST one of url/uds/command; this adds the
            # at-least-one requirement.
            if self.mcp.url is None and self.mcp.uds is None and self.mcp.command is None:
                raise ValueError(f"kind {self.kind.value!r} 'mcp' must declare a transport (url/uds/command)")
            if self.module is not None:
                raise ValueError(f"kind {self.kind.value!r} must not set 'module'")
        else:
            if self.module is None:
                raise ValueError(f"kind {self.kind.value!r} requires 'module'")
            if self.mcp is not None:
                raise ValueError(f"kind {self.kind.value!r} must not set 'mcp'")
        if self.kind is PluginItemKind.ROUTER:
            if self.routes is None:
                raise ValueError(f"kind {self.kind.value!r} requires 'routes'")
        elif self.kind is not PluginItemKind.CHANNEL and self.routes is not None:
            raise ValueError(f"kind {self.kind.value!r} must not set 'routes'")
        return self


class PluginSpec(BaseModel):
    """The complete, validated content of one ``tai-plugin.yml``.

    Frozen and ``extra="forbid"``: a typo'd key fails validation loudly. The
    listing reference is ``namespace/name`` (:attr:`ref`); ``package`` is the
    normalized pip distribution the listing points at; ``version`` must equal
    the built wheel's version (each plugin repo's spec test and the registry's
    ingest validation both pin that); ``contract`` is the tai42-contract
    compatibility range as a PEP 440 specifier set, required unless every
    provided item is kind ``mcp-server`` (such a package imports no contract),
    and a spec may not mix ``mcp-server`` with any other kind. ``display_name``
    and ``icon`` are optional marketplace display metadata; ``premium`` marks a
    paid listing.

    ``migrations`` is an OPT-IN, package-relative directory holding the plugin's
    ordered SQL schema chain — absent means the plugin owns no tables and is not
    a migrations plugin (most plugins). The contract validates only the path's
    SHAPE; the directory's existence in the installed package is enforced at
    runner-discovery time, not here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal[1]
    namespace: str
    name: str
    display_name: str | None = None
    package: str
    version: str
    description: str
    icon: str | None = None
    premium: bool = False
    license: str
    homepage: str | None = None
    repository: str | None = None
    contract: str | None = None
    categories: list[str]
    tags: list[str] = Field(default_factory=list)
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    provides: list[PluginItem]
    migrations: str | None = None

    @property
    def ref(self) -> str:
        """The full listing reference, ``namespace/name``."""
        return f"{self.namespace}/{self.name}"

    @field_validator("namespace", "name")
    @classmethod
    def _check_listing_slug(cls, value: str) -> str:
        if not LISTING_SLUG_RE.fullmatch(value):
            raise ValueError(f"namespace/name must match {LISTING_SLUG_RE.pattern}, got {value!r}")
        return value

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _check_one_line(value, field="display_name")
        if len(value) > DISPLAY_NAME_MAX_LEN:
            raise ValueError(f"display_name must be at most {DISPLAY_NAME_MAX_LEN} characters, got {len(value)}")
        return value

    @field_validator("package")
    @classmethod
    def _check_package(cls, value: str) -> str:
        if not PACKAGE_RE.fullmatch(value):
            raise ValueError(f"package {value!r} must be the normalized pip distribution name")
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not VERSION_RE.fullmatch(value):
            raise ValueError(f"version {value!r} is not a valid PEP 440 version")
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        return _check_one_line(value)

    @field_validator("icon")
    @classmethod
    def _check_icon(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("https://"):
            return _check_web_url(value, field="icon", schemes=("https",))
        if not ICON_RE.fullmatch(value):
            raise ValueError(
                f"icon {value!r} must be an https:// URL or a relative POSIX path "
                "(no leading '/', no drive, no backslash)"
            )
        if ".." in value.split("/"):
            raise ValueError(f"icon path {value!r} must not contain a '..' segment")
        return value

    @field_validator("license")
    @classmethod
    def _check_license(cls, value: str) -> str:
        if not LICENSE_RE.fullmatch(value):
            raise ValueError(f"license {value!r} must be an SPDX license id")
        return value

    @field_validator("homepage", "repository")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_web_url(value, field="homepage/repository", schemes=("http", "https"))

    @field_validator("contract")
    @classmethod
    def _check_contract_range(cls, value: str | None) -> str | None:
        # Presence is the model validator's concern (required unless every
        # provided item is mcp-server); this checks only shape when a value is
        # given. The stored value must already be clean: the clause-stripping
        # below is only to parse each PEP 440 operator, so a
        # surrounding-or-embedded newline / control char or leading/trailing
        # whitespace would validate yet be stored verbatim. Reject it here (inner
        # whitespace around commas, e.g. ``">=0.1, <0.2"``, stays allowed).
        if value is None:
            return None
        if _has_disallowed_control_char(value) or value != value.strip():
            raise ValueError("contract must be a single line with no leading, trailing, or embedded whitespace")
        clauses = [clause.strip() for clause in value.split(",")]
        if "" in clauses:
            raise ValueError("contract must be a non-empty, comma-separated PEP 440 specifier set")
        for clause in clauses:
            _check_specifier_clause(clause)
        return value

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= 3:
            raise ValueError(f"categories must list 1..3 entries, got {len(value)}")
        if len(set(value)) != len(value):
            raise ValueError("categories must be unique")
        for category in value:
            if not LISTING_SLUG_RE.fullmatch(category):
                raise ValueError(f"category {category!r} must match {LISTING_SLUG_RE.pattern}")
        return value

    @field_validator("tags")
    @classmethod
    def _check_spec_tags(cls, value: list[str]) -> list[str]:
        return _check_tags(value)

    @field_validator("provides")
    @classmethod
    def _check_provides(cls, value: list[PluginItem]) -> list[PluginItem]:
        if not value:
            raise ValueError("provides must name at least one item")
        seen: set[tuple[PluginItemKind, str]] = set()
        for item in value:
            key = (item.kind, item.name)
            if key in seen:
                raise ValueError(f"provides has duplicate item {item.kind.value}/{item.name}")
            seen.add(key)
        return value

    @field_validator("migrations")
    @classmethod
    def _check_migrations(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not MIGRATIONS_DIR_RE.fullmatch(value):
            raise ValueError(
                f"migrations {value!r} must be a package-relative POSIX directory path "
                "(no leading '/', no trailing '/', no drive, no backslash)"
            )
        if ".." in value.split("/"):
            raise ValueError(f"migrations path {value!r} must not contain a '..' segment")
        return value

    @model_validator(mode="after")
    def _contract_by_kind(self) -> PluginSpec:
        # One package is one thing: an mcp-server package imports no
        # tai42-contract, so a declared range would be fiction; any other kind
        # needs one. A spec may not mix the two.
        kinds = {item.kind for item in self.provides}
        if PluginItemKind.MCP_SERVER in kinds and len(kinds) > 1:
            others = sorted(kind.value for kind in kinds if kind is not PluginItemKind.MCP_SERVER)
            raise ValueError(
                f"kind {PluginItemKind.MCP_SERVER.value!r} may not share a spec with other kinds; also found {others}"
            )
        if kinds == {PluginItemKind.MCP_SERVER}:
            if self.contract is not None:
                raise ValueError(f"an all-{PluginItemKind.MCP_SERVER.value} spec must not declare 'contract'")
        elif self.contract is None:
            raise ValueError(
                f"'contract' is required unless every provided item is kind {PluginItemKind.MCP_SERVER.value!r}"
            )
        return self

    @model_validator(mode="after")
    def _no_route_collisions(self) -> PluginSpec:
        # One-owner-per-route within the spec: two declared rows collide when
        # their resolved shapes overlap AND their method sets intersect. Same
        # shape with disjoint methods is legal. Plugin declarations never carry
        # converters, so a literal/template overlap is the whole rule here.
        declared: list[tuple[str, str, tuple[str | None, ...], frozenset[str]]] = []
        for item in self.provides:
            if item.routes is None:
                continue
            base = item.routes.base
            for route in item.routes.paths:
                shape = _route_shape(base, route.path)
                methods = frozenset(route.methods)
                for other_name, other_path, other_shape, other_methods in declared:
                    shared = methods & other_methods
                    if shared and _shapes_overlap(shape, other_shape):
                        raise ValueError(
                            f"route collision: item {item.name!r} path {route.path!r} overlaps "
                            f"item {other_name!r} path {other_path!r} on methods {sorted(shared)}"
                        )
                declared.append((item.name, route.path, shape, methods))
        return self


__all__ = [
    "KIND_MANIFEST_BINDINGS",
    "ManifestBinding",
    "PluginItem",
    "PluginItemKind",
    "PluginPermissions",
    "PluginSpec",
    "RouteDecl",
    "RouteMethod",
    "RoutesDecl",
]
