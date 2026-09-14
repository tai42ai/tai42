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
a ``connectors`` descriptor-list append, or no manifest field at all for the
env-selected ``config`` kind).

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

from tai42_contract.plugins.bindings import (
    DATA_BLOCK_BY_KIND,
    KIND_MANIFEST_BINDINGS,
    ManifestBinding,
    PluginItemKind,
    data_kinds,
)

# The listing-slug and tag field-shape patterns are part of the plugins attribute surface
# (the CLI reuses the slug rule; the tool-meta overlay reuses the tag rule) though not in the
# ``*`` export; the explicit-alias re-export keeps them importable from this package.
from tai42_contract.plugins.field_validation import LISTING_SLUG_RE as LISTING_SLUG_RE
from tai42_contract.plugins.field_validation import TAG_RE as TAG_RE
from tai42_contract.plugins.item import PluginItem

# ``ROUTE_BASE_SEGMENT_RE`` is part of the plugins attribute surface (the marketplace
# route mount reuses the base-segment rule) though not in the ``*`` export; the
# explicit-alias re-export keeps it importable from this package.
from tai42_contract.plugins.routes import ROUTE_BASE_SEGMENT_RE as ROUTE_BASE_SEGMENT_RE
from tai42_contract.plugins.routes import RouteDecl, RouteMethod, RoutesDecl
from tai42_contract.plugins.spec import PluginPermissions, PluginSpec

__all__ = [
    "DATA_BLOCK_BY_KIND",
    "KIND_MANIFEST_BINDINGS",
    "ManifestBinding",
    "PluginItem",
    "PluginItemKind",
    "PluginPermissions",
    "PluginSpec",
    "RouteDecl",
    "RouteMethod",
    "RoutesDecl",
    "data_kinds",
]
