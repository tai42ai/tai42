"""Resolve a spec's declared route mounts, gate them, and build the preview response body.

The gate is applied BEFORE any state change: an unknown-item / bad-base override is
a 400, a resolved PUBLIC route under a reserved prefix or a collision against the
live registry is refused, and unaccepted public rows are refused. The live registry
and the reserved-prefix set are reached through injected callables so a test drives
the gate deterministically.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from tai42_contract.plugins import PluginSpec
from tai42_kit.plugins import required_env_for_spec

from tai42_skeleton.marketplace import routes as routes_mod
from tai42_skeleton.marketplace.errors import (
    PublicRoutesNotAcceptedError,
    ReservedRoutePrefixError,
    RouteCollisionError,
)
from tai42_skeleton.marketplace.resolve import spec_from_row
from tai42_skeleton.marketplace.store import InstallRecord


def live_owned_routes() -> list[routes_mod.OwnedRoute]:
    """The live route registry's committed ``/api`` ownership generation.

    The routes a candidate install/update is collision-checked against.
    """
    from tai42_skeleton.app.route_registry import route_registry

    return routes_mod.owned_routes_from_registry(route_registry)


def live_reserved_prefixes() -> Sequence[str]:
    """The deployment's reserved never-public route prefixes.

    A resolved public route may not mount under any of them.
    """
    from tai42_skeleton.access_control.settings import access_control_settings

    return access_control_settings().reserved_public_pin_prefixes


def route_preflight(
    spec: PluginSpec,
    route_mounts: Mapping[str, str] | None,
    accept_public_routes: bool,
    *,
    prior: Mapping[str, str] | None,
    exclude_ref: str | None,
    approved_public: set[tuple[str, tuple[str, ...]]] | None,
    owned_routes: Callable[[], list[routes_mod.OwnedRoute]],
    reserved_prefixes: Callable[[], Sequence[str]],
) -> tuple[dict[str, str], list[routes_mod.ResolvedRoute]]:
    """Resolve the spec's declared route mounts and refuse before any state change.

    Applies ``route_mounts`` overrides over ``prior`` stored bases and the
    declared defaults (:func:`~tai42_skeleton.marketplace.routes.resolve_mounts`,
    an unknown-item / bad-base override → 400), rejects a resolved PUBLIC route
    under a reserved prefix, collision-checks every resolved route against the live
    registry EXCLUDING ``exclude_ref``'s own routes (a clash → 409 ``ROUTE_COLLISION``
    naming the remap remedy), and refuses unaccepted public routes: an install
    (``approved_public`` is ``None``) requires acceptance of EVERY public row, an
    update only of rows not already approved. Returns the resolved
    ``{item_name: base}`` and the resolved routes for the receipt.
    """
    mounts = routes_mod.resolve_mounts(spec, route_mounts, prior=prior)
    resolved = routes_mod.resolved_routes(spec, mounts)
    reserved = list(reserved_prefixes())
    offenders = routes_mod.reserved_public_offenders(resolved, reserved)
    if offenders:
        raise ReservedRoutePrefixError([route.full_path for route in offenders], reserved)
    found = routes_mod.find_collisions(resolved, owned_routes(), exclude_ref=exclude_ref)
    if found:
        raise RouteCollisionError(found)
    public = routes_mod.public_rows(resolved)
    if approved_public is None:
        need_acceptance = public
    else:
        need_acceptance = [row for row in public if routes_mod.row_key(row) not in approved_public]
    if need_acceptance and not accept_public_routes:
        raise PublicRoutesNotAcceptedError(need_acceptance)
    return mounts, resolved


def approved_public_of(row: InstallRecord) -> set[tuple[str, tuple[str, ...]]]:
    """The public route rows the installed version already approved.

    Its stored spec resolved at its stored mount bases. An update asks acceptance
    only for public rows not in this set.
    """
    old_spec = spec_from_row(row)
    old_mounts = routes_mod.resolve_mounts(old_spec, {}, prior=row.route_mounts)
    old_resolved = routes_mod.resolved_routes(old_spec, old_mounts)
    return {routes_mod.row_key(r) for r in routes_mod.public_rows(old_resolved)}


def build_route_preview(
    spec: PluginSpec,
    pinned_version: str,
    route_mounts: dict[str, str] | None,
    existing: InstallRecord | None,
    *,
    owned_routes: Callable[[], list[routes_mod.OwnedRoute]],
    reserved_prefixes: Callable[[], Sequence[str]],
    effective_env: Mapping[str, Any],
) -> dict[str, Any]:
    """The preview response body.

    The resolved routes per item, collisions against the live registry (excluding
    the plugin's OWN routes on an update preview), the public rows, the ``new``
    public rows not already approved, and the env picture (``required_env`` /
    ``missing_env`` computed against the resolved ``effective_env`` the caller
    supplies) plus ``delivery``.
    """
    prior = existing.route_mounts if existing is not None else None
    mounts = routes_mod.resolve_mounts(spec, route_mounts, prior=prior)
    resolved = routes_mod.resolved_routes(spec, mounts)
    reserved = list(reserved_prefixes())
    offenders = routes_mod.reserved_public_offenders(resolved, reserved)
    if offenders:
        raise ReservedRoutePrefixError([route.full_path for route in offenders], reserved)
    exclude_ref = spec.ref if existing is not None else None
    found = routes_mod.find_collisions(resolved, owned_routes(), exclude_ref=exclude_ref)
    public = routes_mod.public_rows(resolved)
    if existing is not None:
        approved = approved_public_of(existing)
        new_public = [row for row in public if routes_mod.row_key(row) not in approved]
    else:
        new_public = public
    # The env picture, computed server-side so no client re-derives it: every var the
    # spec requires with its derived secret-ness, and which of them is not already
    # present in the resolved effective env (the SAME rule the installer's pre-check
    # and the config pipeline's refusal apply). ``delivery`` is the one word every
    # surface shows (``package`` / ``descriptor``).
    required = required_env_for_spec(spec)
    return {
        "ref": spec.ref,
        "version": pinned_version,
        "items": routes_mod.preview_items(resolved),
        "collisions": found,
        "public_routes": public,
        "new_public_routes": new_public,
        "requires_public_acceptance": bool(new_public),
        "required_env": [{"name": req.name, "secret": req.secret} for req in required],
        "missing_env": [req.name for req in required if req.name not in effective_env],
        "delivery": spec.delivery,
    }
