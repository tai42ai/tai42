"""Response models for the marketplace operations: inventory, advisories, install, uninstall and upgrade."""

from __future__ import annotations

from pydantic import BaseModel, JsonValue
from tai42_contract.app.responses import ApplyResponse
from tai42_contract.plugins import PluginDelivery, RouteMethod

from tai42_skeleton.marketplace.compat import CompatState, UpgradeOutcome


class CompatStatus(BaseModel):
    """An installed distribution's compatibility verdict against the running contract."""

    status: CompatState
    reason: str | None


class ItemRef(BaseModel):
    """One item a plugin's stored spec provides."""

    kind: str
    name: str


class InstalledRow(BaseModel):
    """One installed plugin with its update picture computed against the running contract.

    ``installed_at`` is ISO-8601; ``latest`` / ``incompatible_newer`` are ``null`` when none applies;
    ``delivery`` is ``package`` or ``descriptor``; ``route_mounts`` maps each route-carrying item name to
    its mounted base.
    """

    ref: str
    version: str
    source: str
    delivery: PluginDelivery
    installed_at: str
    latest: str | None
    update_available: bool
    incompatible_newer: str | None
    missing_upstream: bool
    compat: CompatStatus
    items: list[ItemRef]
    route_mounts: dict[str, str]


class InstalledInventory(BaseModel):
    """The installed inventory."""

    installed: list[InstalledRow]


class AdvisorySnapshot(BaseModel):
    """The advisory snapshot for the installed plugins.

    Each advisory row is an upstream registry object forwarded verbatim (its shape is owned by the
    registry); ``fetched_at`` is the ISO-8601 fetch time.
    """

    advisories: list[dict[str, JsonValue]]
    fetched_at: str


class MountedRoute(BaseModel):
    """One route an install/update mounted."""

    item: str
    full_path: str
    methods: list[RouteMethod]
    public: bool


class RouteCollision(BaseModel):
    """A declared route that clashes with an already-owned one."""

    item: str
    full_path: str
    methods: list[RouteMethod]
    conflict_owner: str
    conflict_path: str


class PublicRouteRow(BaseModel):
    """A public route as an acceptance row."""

    item: str
    full_path: str
    methods: list[RouteMethod]


class RequiredEnvVar(BaseModel):
    """An env var a plugin spec requires, with its derived secret-ness."""

    name: str
    secret: bool


class PreviewItemRoute(BaseModel):
    """One resolved route of a preview's route-carrying item."""

    path: str
    full_path: str
    methods: list[RouteMethod]
    public: bool


class PreviewItem(BaseModel):
    """One route-carrying item in an install/update preview, with its resolved and declared bases and routes."""

    item: str
    kind: str
    base: str
    default_base: str
    routes: list[PreviewItemRoute]


class InstallResult(BaseModel):
    """The install (and update) receipt.

    ``package`` is ``null`` for a descriptor-only plugin; ``advisories`` are the target's upstream advisory
    rows forwarded verbatim; ``notes`` are activation notes; ``reload`` is the manifest apply's fleet
    result; ``pip_output`` is ``null`` for a descriptor-only plugin; ``routes`` lists every route the
    operation mounted.
    """

    ref: str
    version: str
    package: str | None
    advisories: list[JsonValue]
    notes: list[str]
    reload: ApplyResponse
    pip_output: str | None
    routes: list[MountedRoute]


class InstallPreview(BaseModel):
    """A no-side-effect install/update preview.

    The resolved routes per item, the collisions against the live registry, the public rows requiring
    acceptance, the ``new_public_routes`` an update has not already approved, the required and missing
    env vars, and the delivery form.
    """

    ref: str
    version: str
    items: list[PreviewItem]
    collisions: list[RouteCollision]
    public_routes: list[PublicRouteRow]
    new_public_routes: list[PublicRouteRow]
    requires_public_acceptance: bool
    required_env: list[RequiredEnvVar]
    missing_env: list[str]
    delivery: PluginDelivery


class UninstallResult(BaseModel):
    """The uninstall receipt.

    ``reload`` is ``null`` when the plugin wrote no manifest entry to remove; ``notes`` are removal notes
    (e.g. orphaned env vars left in place).
    """

    ref: str
    uninstalled: bool
    reload: ApplyResponse | None
    notes: list[str]


class UpgradeRow(BaseModel):
    """One ref's upgrade-all outcome.

    ``outcome`` is one of ``upgraded`` / ``up-to-date`` / ``no-compatible-version`` / ``failed``;
    ``detail`` is its human-readable note. ``span_release_error`` is ``null`` when the ref wrote nothing
    or the config store released its exclusive write span after each config write the ref landed;
    otherwise the store's errors, joined with ``"; "`` in write order — an ``upgraded`` ref's manifest
    write, or a ``failed`` ref's forward write and the restore writes of its unwind. Those writes stand,
    but the store may still hold its write span until the writer process exits, so a later write can
    block or be refused until then.
    """

    ref: str
    outcome: UpgradeOutcome
    detail: str
    span_release_error: str | None


class UpgradeAllResult(BaseModel):
    """The complete per-ref upgrade-all report."""

    results: list[UpgradeRow]
