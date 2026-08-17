"""Resolve, collision-check, and gate a plugin's declared HTTP routes at install.

Pure, side-effect-free functions over a validated :class:`PluginSpec` and its
route-mount overrides — no I/O, no app handle — so the installer's route
pre-flight and the preview door share one algebra and each piece is unit-testable
in isolation.

An item's declared routes mount under a per-item base: the resolved absolute path
is ``/api/`` + the item's mount base + the declared relative path. The base is the
declaration's default unless the operator remapped it (or, on an update, the base
already stored for a surviving item). One-owner-per-route is enforced by the same
path-SHAPE algebra the registry uses (:mod:`tai42_skeleton.app.route_shapes`):
a candidate route collides when its shape overlaps an already-owned route's shape
AND their method sets intersect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tai42_contract.plugins import ROUTE_BASE_SEGMENT_RE, PluginItem, PluginSpec

from tai42_skeleton.access_control.path_canon import canonicalize_path
from tai42_skeleton.app.route_shapes import Shape, collision, parse_shape
from tai42_skeleton.marketplace.errors import RouteMountError

# The fixed platform-wide route root; only an item's mount base is remappable.
_API_ROOT = "/api/"


@dataclass(frozen=True)
class ResolvedRoute:
    """One declared route with its mount base applied: the owning ``item`` and its
    ``kind``, the resolved ``base`` (override, stored, or default) and the
    ``default_base`` it declares, the relative ``path``, its resolved ``full_path``,
    the ``methods``, and whether it is ``public`` (answers unauthenticated)."""

    item: str
    kind: str
    base: str
    default_base: str
    path: str
    full_path: str
    methods: tuple[str, ...]
    public: bool


@dataclass(frozen=True)
class OwnedRoute:
    """One ``/api`` route the live registry already owns: its parsed ``shape`` and
    served ``methods``, the ``owner_label`` (``core`` or ``plugin:<ref>``), the
    owning ``owner_ref`` (``None`` for core), and the registered ``path`` — the
    identity a candidate route is collision-checked against."""

    shape: Shape
    methods: frozenset[str]
    owner_label: str
    owner_ref: str | None
    path: str


def _route_carrying_items(spec: PluginSpec) -> dict[str, PluginItem]:
    """The spec's items that declare HTTP routes, keyed by item name (a router
    always declares routes, a channel optionally does)."""
    return {item.name: item for item in spec.provides if item.routes is not None}


def _validate_base(base: str, item_name: str) -> None:
    """Reject a mount base that is not the contract's relative base charset —
    a leading/trailing slash or a segment outside :data:`ROUTE_BASE_SEGMENT_RE`."""
    if base.startswith("/") or base.endswith("/"):
        raise RouteMountError(
            f"route_mounts base {base!r} for item {item_name!r} must be relative (no leading or trailing '/')"
        )
    for segment in base.split("/"):
        if not ROUTE_BASE_SEGMENT_RE.fullmatch(segment):
            raise RouteMountError(
                f"route_mounts base segment {segment!r} for item {item_name!r} must match "
                f"{ROUTE_BASE_SEGMENT_RE.pattern} (no templates)"
            )


def resolve_mounts(
    spec: PluginSpec,
    overrides: Mapping[str, str] | None,
    *,
    prior: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The resolved ``{item_name: base}`` for EVERY route-carrying item.

    An override wins; else a ``prior`` stored base (an update keeps a surviving
    item's mount); else the declaration's default base. An override naming an item
    that is not route-carrying, or a base outside the mount charset, is a loud
    :class:`RouteMountError` (the caller's 400).
    """
    items = _route_carrying_items(spec)
    overrides = overrides or {}
    for name, base in overrides.items():
        if name not in items:
            raise RouteMountError(f"route_mounts names {name!r}, which is not a route-carrying item of {spec.ref}")
        _validate_base(base, name)
    prior = prior or {}
    mounts: dict[str, str] = {}
    for name, item in items.items():
        assert item.routes is not None  # guaranteed by _route_carrying_items
        if name in overrides:
            mounts[name] = overrides[name]
        elif name in prior:
            mounts[name] = prior[name]
        else:
            mounts[name] = item.routes.base
    return mounts


def resolved_routes(spec: PluginSpec, mounts: Mapping[str, str]) -> list[ResolvedRoute]:
    """Every declared route of the spec resolved against ``mounts`` (which must
    cover every route-carrying item — see :func:`resolve_mounts`)."""
    out: list[ResolvedRoute] = []
    for item in spec.provides:
        if item.routes is None:
            continue
        base = mounts[item.name]
        default_base = item.routes.base
        for route in item.routes.paths:
            out.append(
                ResolvedRoute(
                    item=item.name,
                    kind=item.kind.value,
                    base=base,
                    default_base=default_base,
                    path=route.path,
                    full_path=f"{_API_ROOT}{base}{route.path}",
                    methods=tuple(route.methods),
                    public=route.public,
                )
            )
    return out


def owned_routes_from_registry(registry: Any) -> list[OwnedRoute]:
    """The live registry's committed ``/api`` shape generation as
    :class:`OwnedRoute` rows — the ownership set a candidate is checked against."""
    result: list[OwnedRoute] = []
    for entry in registry.api_shape_index():
        owner = entry.meta.owner
        if owner.kind == "plugin":
            label = f"plugin:{owner.owner_ref}"
            ref = owner.owner_ref
        else:
            label = "core"
            ref = None
        result.append(
            OwnedRoute(
                shape=entry.shape,
                methods=entry.methods,
                owner_label=label,
                owner_ref=ref,
                path=entry.meta.path,
            )
        )
    return result


def find_collisions(
    routes: Sequence[ResolvedRoute],
    owned: Sequence[OwnedRoute],
    *,
    exclude_ref: str | None,
) -> list[dict[str, Any]]:
    """Each candidate route that collides (shape overlap + method intersection)
    with an already-owned route, as ``{item, full_path, methods, conflict_owner,
    conflict_path}``. Routes owned by ``exclude_ref`` are skipped — an update
    excludes the plugin's OWN currently-installed routes so a surviving route never
    self-collides. At most one clash is reported per candidate (the first found)."""
    out: list[dict[str, Any]] = []
    for route in routes:
        candidate = parse_shape(route.full_path)
        candidate_methods = frozenset(route.methods)
        for owner in owned:
            if exclude_ref is not None and owner.owner_ref == exclude_ref:
                continue
            if collision(candidate, candidate_methods, owner.shape, owner.methods):
                out.append(
                    {
                        "item": route.item,
                        "full_path": route.full_path,
                        "methods": list(route.methods),
                        "conflict_owner": owner.owner_label,
                        "conflict_path": owner.path,
                    }
                )
                break
    return out


def reserved_public_offenders(
    routes: Sequence[ResolvedRoute],
    reserved_prefixes: Sequence[str],
) -> list[ResolvedRoute]:
    """The public routes that resolve under a reserved never-public prefix (a
    resolved path equal to a prefix or nested beneath it)."""
    offenders: list[ResolvedRoute] = []
    for route in routes:
        if not route.public:
            continue
        # Canonicalize (dot-resolution + slash-collapse) BEFORE the prefix test so a
        # non-canonical resolved path cannot dodge a reserved prefix it only falls
        # under after normalization — "reserved always wins", matching the runtime
        # verifier and the boot mount-map audit.
        full_path = canonicalize_path(route.full_path)
        if any(full_path == prefix or full_path.startswith(f"{prefix}/") for prefix in reserved_prefixes):
            offenders.append(route)
    return offenders


def public_rows(routes: Sequence[ResolvedRoute]) -> list[dict[str, Any]]:
    """The public routes as acceptance rows: ``{item, full_path, methods}``."""
    return [
        {"item": route.item, "full_path": route.full_path, "methods": list(route.methods)}
        for route in routes
        if route.public
    ]


def mounted_rows(routes: Sequence[ResolvedRoute]) -> list[dict[str, Any]]:
    """The install/update receipt's route list: ``{item, full_path, methods,
    public}`` for every mounted route (empty when the plugin declares none)."""
    return [
        {"item": route.item, "full_path": route.full_path, "methods": list(route.methods), "public": route.public}
        for route in routes
    ]


def preview_items(routes: Sequence[ResolvedRoute]) -> list[dict[str, Any]]:
    """The preview's per-item projection: one entry per route-carrying item with
    its resolved ``base``, declared ``default_base``, and its routes."""
    items: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for route in routes:
        entry = index.get(route.item)
        if entry is None:
            entry = {
                "item": route.item,
                "kind": route.kind,
                "base": route.base,
                "default_base": route.default_base,
                "routes": [],
            }
            index[route.item] = entry
            items.append(entry)
        entry["routes"].append(
            {
                "path": route.path,
                "full_path": route.full_path,
                "methods": list(route.methods),
                "public": route.public,
            }
        )
    return items


def row_key(row: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """The identity of an acceptance row for delta comparison across versions: its
    resolved ``full_path`` and sorted ``methods``."""
    return (row["full_path"], tuple(sorted(row["methods"])))
