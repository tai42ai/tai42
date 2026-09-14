"""Route catalog + public-pin doors: list routes, list/pin/unpin public routes."""

from __future__ import annotations

from typing import Any

from starlette.routing import Mount, Route

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control import management
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError, operation
from tai42_skeleton.operations.response_models_group_a import RouteMappingList, StringList, UrlAck

from .models import _DISABLED_CODE, _DISABLED_MESSAGE, PublicRoutePin, PublicRouteUnpin


@operation(
    summary="List the app's HTTP routes and their scope mappings",
    tags=["access-control"],
    response_model=RouteMappingList,
)
async def list_routes(routes: list[Any]) -> list[dict[str, Any]]:
    """Enumerate the app's own HTTP routes with each route's current scope mapping —
    the mapper's route picker and its "unassigned routes" bucket (the ``mapped: null``
    entries).

    ``routes`` is the app's live route table (the route-adapter extractor hands the
    operation ``request.app.routes`` so it stays request-free). One entry per
    :class:`starlette.routing.Route`, sorted by ``path``. ``Mount`` entries (the sub-MCP
    mount, the MCP mount) are EXCLUDED — a mount is not a bindable url and the sub-MCP
    surface has its own door. The exclusion is an explicit ``isinstance`` filter: any
    route-table entry that is neither a ``Route`` nor a ``Mount`` raises loudly (a new
    Starlette routing type must be classified here, never silently dropped). ``methods``
    is the route's method set sorted with ``HEAD`` removed (Starlette auto-adds it to
    every GET route — noise for the mapper). ``mapped`` is the url's value from
    ``get_all_route_mappings`` looked up by the EXACT path string — a scope id, the
    public marker for a public pin, or ``null`` when the path has no mapping. Exact-key
    lookup only: this door does not attempt dynamic-pattern matching.

    Each entry is JOINED with its :class:`RouteMetadata` (from
    ``route_registry.load_all_routes()``, a separate collection) to also carry the
    route's feature ``tags`` + ``summary`` + authorization ``action`` — the data the
    Studio Roles page groups the per-tag tri-state by and marks the ``fenced``/``secret``
    (admin-only) routes it must never offer a grant for. The join is keyed on the route
    TEMPLATE + its method set with ``HEAD`` stripped on BOTH sides (Starlette auto-adds
    ``HEAD`` to a GET), so the two collections compare like-for-like; a registered route
    whose method set fails to join is a loud STOP (a normalization drift), never a
    silently dropped row."""
    # OFF: access control disabled → no scope mappings exist, so the honest answer
    # is the empty route list (no AC store touched under the synthetic admin).
    if not _pkg.access_control_settings().enable:
        return []
    from tai42_skeleton.app.route_registry import load_all_routes

    mappings = await management.get_all_route_mappings()
    # MOUNTED surfaces (the MCP transports, the sub-MCP mount) are dropped before the
    # join: they carry no tags and no grant can ever open them, so they join nothing and
    # must not turn the ``registered_paths`` drift check against their own live route.
    all_meta = [meta for meta in load_all_routes() if not meta.mounted]
    meta_by_key = {(meta.path, frozenset(m for m in meta.methods if m != "HEAD")): meta for meta in all_meta}
    registered_paths = {meta.path for meta in all_meta}
    entries: list[dict[str, Any]] = []
    for route in routes:
        if isinstance(route, Mount):
            continue
        if not isinstance(route, Route):
            raise TypeError(
                f"unclassified route-table entry {type(route).__name__!r}: {route!r} — a new starlette "
                "routing type must be classified in list_routes, not silently dropped"
            )
        methods = sorted(m for m in (route.methods or set()) if m != "HEAD")
        entry: dict[str, Any] = {"path": route.path, "methods": methods, "mapped": mappings.get(route.path)}
        meta = meta_by_key.get((route.path, frozenset(methods)))
        if meta is not None:
            entry.update(tags=list(meta.tags), summary=meta.summary, action=meta.action)
        elif route.path in registered_paths:
            # The PATH is a registered route but this method set did not join — a genuine
            # method-normalization drift between the two collections. A loud STOP, never a
            # silently dropped metadata row.
            raise TypeError(
                f"gated route {'/'.join(methods)} {route.path} did not join its route-registry metadata — "
                "the method-set normalization drifted; a gated route must never be a dropped row"
            )
        else:
            # A path not in the registry at all carries no metadata to join (the boot
            # action-class audit guarantees every registered gated route is classified).
            entry.update(tags=[], summary="", action=None)
        entries.append(entry)
    entries.sort(key=lambda entry: entry["path"])
    return entries


@operation(summary="List public-pinned routes", tags=["access-control"], response_model=StringList)
async def list_public_routes() -> list[str]:
    """Every route pinned to the public marker."""
    # OFF: access control disabled → no pins exist; the honest empty list.
    if not _pkg.access_control_settings().enable:
        return []
    return await management.get_public_route_pins()


@operation(
    summary="Pin a route public",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, NotSupportedError],
    request_model=PublicRoutePin,
    response_model=UrlAck,
)
async def pin_public_route(url: str, pattern: str | None) -> dict[str, str]:
    """Pin ``url`` public (optionally with a dynamic match ``pattern``)."""
    # OFF: access control disabled → refuse the pin with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    try:
        await management.pin_route_public(url, pattern)
    except ValueError as exc:
        # A url under a reserved management prefix cannot be pinned public — the
        # control plane must not be usable to de-authenticate itself.
        raise BadRequestError(str(exc)) from exc
    # Bump AFTER the write so the version-keyed enforcer route cache re-reads the pin.
    await management.bump_policy_version()
    return {"url": url}


@operation(
    summary="Unpin a public route",
    tags=["access-control"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=PublicRouteUnpin,
    response_model=UrlAck,
)
async def unpin_public_route(url: str) -> dict[str, str]:
    """Unpin a public ``url``; a url that is absent or scope-mapped is a loud 404."""
    # OFF: access control disabled → refuse the unpin with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    if not await management.unpin_public_route(url):
        raise NotFoundError(f"url is not pinned public: {url!r}")
    await management.bump_policy_version()
    return {"url": url}
