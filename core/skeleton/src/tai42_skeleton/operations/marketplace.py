"""Marketplace operations — search, browse, and manage installed plugins.

Eleven operations back the ``/api/marketplace/*`` surface: seven reads (search the
registry, one listing's detail with its versions, the category vocabulary, the
item-kind vocabulary, the installed inventory with per-row compat/update
availability plus the boot's plugin-quarantine entries, the advisory snapshot, and
a no-side-effect install/update route preview) and four environment-mutating flows
(install, uninstall, update, upgrade-all) driven by
:class:`~tai42_skeleton.marketplace.installer.Installer`.

The internal :class:`~tai42_skeleton.marketplace.errors.MarketplaceError` family is
load-bearing inside the marketplace package (it carries the not-installed flag,
the pip argv/return code, the two errors of a failed unwind). At THIS boundary
each is translated to the shared operation-error vocabulary
(:func:`_to_operation_error`): a dead or garbled registry is an
:class:`UpstreamError` (502 — this surface proxies the registry, so an upstream
failure is never a this-server 500); an in-progress operation elsewhere in the
fleet is an :class:`UnavailableError` (503, retriable); an unknown listing or a
not-installed ref is a :class:`NotFoundError` (404); a version/collision/contract,
already-installed, or environment-shadowed-prefix conflict is a
:class:`ConflictError` (409); a pip failure,
a github artifact-integrity mismatch, a failed unwind, a manifest-compose fault,
an unknown-item-kind binding drift, or corrupt local
state is an :class:`OperationFailed` (500). A malformed ``ref`` is the caller's
own author error — a typed :class:`~tai42_skeleton.marketplace.errors.MalformedRefError`
the boundary maps to :class:`BadRequestError` (400).

Retry-After limitation: the 503 an in-progress operation answers carries no
``Retry-After`` header. The route adapter stamps static headers on the SUCCESS
response only; an error response is header-less by construction, and the typed
error's ``extra`` dict merges into the JSON body, not the headers. The retriable
signal therefore rides the message and the 503 status, not a header. (The
reload-gate 503 — a separate concern the adapter honors from ``reload_gated``
metadata — DOES carry ``Retry-After: 5``, since that response is built by the
reload gate itself, not the error path.)

install/uninstall/update/upgrade-all mutate the running environment by running
arbitrary third-party code, so each is ``destructive`` and ``authority_changing``
(off the default MCP tool surface — tier 2 — includable only by an explicit
``api_tools.include``) and ``reload_gated`` (the flow ends in a manifest reload,
so the adapter answers a retriable 503 while a reload is in flight).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from tai42_kit.db import component_store_configured

from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.marketplace import advisories
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.compat import (
    CompatVerdict,
    UpdateTargets,
    dist_compat,
    running_contract_version,
    update_targets,
)
from tai42_skeleton.marketplace.errors import (
    ArtifactIntegrityError,
    ContractIncompatibleError,
    EnvironmentShadowError,
    InstallEnvError,
    InstallStateError,
    ListingNotFoundError,
    MalformedRefError,
    ManifestCollisionError,
    MarketplaceError,
    OperationInProgressError,
    PipFailedError,
    PublicRoutesNotAcceptedError,
    RegistryResponseError,
    RegistryUnreachableError,
    ReservedRoutePrefixError,
    RouteCollisionError,
    RouteMountError,
    VersionRefusedError,
)
from tai42_skeleton.marketplace.installer import Installer
from tai42_skeleton.marketplace.settings import marketplace_settings
from tai42_skeleton.marketplace.store import MarketplaceInstallStore
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationError,
    OperationFailed,
    UnavailableError,
    UpstreamError,
    operation,
)
from tai42_skeleton.plugins.quarantine import quarantined_plugins

logger = logging.getLogger(__name__)

# The machine-readable code every marketplace mutation OFF refusal carries when the
# install-attribution store is unconfigured; the message is rendered from the live
# binding at raise time. Hoisted so the four mutation refusals read one way.
_NOT_CONFIGURED_CODE = "marketplace-not-configured"
_NOT_CONFIGURED_NOUN = "marketplace install store"

# Keep the tail of an oversized detail (a pip failure's captured output) for the
# error envelope; the full text is logged whole. The prefix marks a cut.
_ENVELOPE_DETAIL_CHARS = 4000


def _truncate(text: str) -> str:
    """The last ``_ENVELOPE_DETAIL_CHARS`` of ``text``, prefixed with a visible cut
    marker when anything was dropped."""
    if len(text) <= _ENVELOPE_DETAIL_CHARS:
        return text
    return f"... (truncated) {text[-_ENVELOPE_DETAIL_CHARS:]}"


def _provided_items(spec: dict[str, Any]) -> list[dict[str, str]]:
    """The ``{kind, name}`` of every item the stored spec provides, in spec order.

    Read from the locally stored ``PluginSpec`` document (``model_dump(mode="json")``,
    so ``kind`` is its enum string), NOT the registry — the answer is offline truth.
    Studio joins these names against the manifest's ``mcp`` entry titles to mark an
    installer-written entry read-only (the ``mcp_entry`` writer keys the title on the
    provides item ``name``); a malformed/absent provides list yields ``[]``.
    """
    provides = spec.get("provides")
    if not isinstance(provides, list):
        return []
    items: list[dict[str, str]] = []
    for item in provides:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        name = item.get("name")
        if isinstance(kind, str) and isinstance(name, str):
            items.append({"kind": kind, "name": name})
    return items


def _to_operation_error(exc: MarketplaceError) -> OperationError:
    """Translate an internal marketplace failure to its honest operation error.

    The classification keys on the typed class (and, for a state conflict, its
    ``not_installed`` flag) — never on message text.
    """
    if isinstance(exc, MalformedRefError | InstallEnvError | RouteMountError):
        # The caller's own author error — a ref that is not a well-formed lowercase
        # ``namespace/name``, an mcp-server install missing a required !ENV value
        # (each missing var + json-pointer named, names only), or a route_mounts
        # override naming a non-route item / a bad base.
        return BadRequestError(str(exc))
    if isinstance(exc, PublicRoutesNotAcceptedError):
        # Declared public routes need the operator's explicit acceptance — a 400
        # carrying a stable code + the rows so the UI can render the acceptance gate.
        return BadRequestError(
            str(exc), extra={"code": "PUBLIC_ROUTES_NOT_ACCEPTED", "public_routes": exc.public_routes}
        )
    if isinstance(exc, RouteCollisionError):
        # A declared route clashes with an already-owned one — a 409 carrying the
        # collision list; the remedy is to remap the item's base.
        return ConflictError(str(exc), extra={"code": "ROUTE_COLLISION", "collisions": exc.collisions})
    if isinstance(exc, ReservedRoutePrefixError):
        # A declared public route resolves under a reserved never-public prefix — a
        # 409 carrying the offending paths; the remedy is to remap the base.
        return ConflictError(str(exc), extra={"code": "ROUTE_RESERVED_PREFIX", "routes": exc.offenders})
    if isinstance(exc, RegistryUnreachableError | RegistryResponseError):
        # The registry is this surface's upstream; a dead/garbled upstream is a
        # 502, never a this-server 500 and never a promise that retry fixes it.
        return UpstreamError(str(exc))
    if isinstance(exc, ListingNotFoundError):
        return NotFoundError(str(exc))
    if isinstance(exc, OperationInProgressError):
        # Another worker (or this one) holds the fleet-wide marketplace lock —
        # retriable. See the module docstring's Retry-After limitation.
        return UnavailableError(str(exc))
    if isinstance(exc, InstallStateError):
        # A not-installed ref is a 404; every other state conflict is a 409.
        return NotFoundError(str(exc)) if exc.not_installed else ConflictError(str(exc))
    if isinstance(
        exc, VersionRefusedError | ManifestCollisionError | ContractIncompatibleError | EnvironmentShadowError
    ):
        # State conflicts the operator resolves, not by retrying as-is (an
        # environment-shadowed prefix version is re-pinned or the image rebuilt).
        return ConflictError(str(exc))
    if isinstance(exc, PipFailedError):
        # The full captured output stays in the log; the envelope carries the
        # summary plus a truncated tail so the operator sees the failing lines.
        logger.error("marketplace pip failure: %s\n%s", exc, exc.output)
        return OperationFailed(str(exc), extra={"pip_output": _truncate(exc.output)})
    if isinstance(exc, ArtifactIntegrityError):
        # A github artifact whose sha256 disagrees with the registry's ingest
        # digest — an install-integrity failure (a possibly re-pointed release
        # tag), a loud terminal 500 carrying the digests and the rejected URL.
        logger.error("marketplace artifact integrity failure: %s", exc)
        return OperationFailed(
            str(exc),
            extra={
                "artifact_ref": exc.artifact_ref,
                "expected_sha256": exc.expected_sha256,
                "actual_sha256": exc.actual_sha256,
            },
        )
    # PipUnavailableError / InstallUnwindError / ManifestComposeError /
    # LocalStateError / the base: the deployment environment failed the operation.
    return OperationFailed(_truncate(str(exc)))


class MarketplaceInstall(BaseModel):
    """Install a marketplace plugin by ref, optionally pinning a version.

    ``env`` / ``secret_keys`` are accepted for an mcp-server install: an mcp entry's
    required ``!ENV`` markers are satisfied by writing these values to the env store
    in the same transaction that writes the entry (never deferred), and ``secret_keys``
    marks the given keys secret (appended to ``TAI_ENV_SECRET_KEYS``). Declared on the
    model so a Studio body carrying them is not silently dropped (pydantic ignores
    unknown fields).

    ``route_mounts`` remaps a route-carrying item's declared base (``{item_name:
    base}``); ``accept_public_routes`` acknowledges that the install's public routes
    answer WITHOUT authentication (required when any public route is declared)."""

    ref: str
    version: str | None = None
    env: dict[str, str] | None = None
    secret_keys: list[str] | None = None
    route_mounts: dict[str, str] | None = None
    accept_public_routes: bool = False


class MarketplaceUninstall(BaseModel):
    """Uninstall a marketplace-installed plugin by ref."""

    ref: str


class MarketplaceUpdate(BaseModel):
    """Update an installed plugin to a newer (or named) version.

    ``env`` / ``secret_keys`` mirror :class:`MarketplaceInstall`: a new mcp-server
    version adding a required marker is loudly refused unless its value is supplied
    here. ``route_mounts`` remaps a route-carrying item's base (a surviving item with
    no override keeps its stored base); ``accept_public_routes`` acknowledges public
    routes NOT already approved in the installed version."""

    ref: str
    version: str | None = None
    env: dict[str, str] | None = None
    secret_keys: list[str] | None = None
    route_mounts: dict[str, str] | None = None
    accept_public_routes: bool = False


class MarketplaceInstallPreview(BaseModel):
    """Preview an install/update by ref: resolve the target spec and report its
    routes with any ``route_mounts`` base overrides applied, WITHOUT changing state."""

    ref: str
    version: str | None = None
    route_mounts: dict[str, str] | None = None


class MarketplaceSearchQuery(BaseModel):
    """The marketplace search door's facets: a repeated ``?tags=`` array plus single-valued
    facets.

    Spec metadata only — the door parses its query at the HTTP edge."""

    q: str | None = Field(default=None, description="Free-text search query.")
    kind: str | None = Field(default=None, description="Restrict to one item kind.")
    category: str | None = Field(default=None, description="Restrict to one category.")
    tags: list[str] = Field(default_factory=list, description="Restrict to these tags; repeat the parameter per tag.")
    namespace: str | None = Field(default=None, description="Restrict to one publisher namespace.")
    tier: str | None = Field(default=None, description="Restrict to one tier.")
    contract: str | None = Field(default=None, description="Restrict to one contract version.")
    sort: str | None = Field(default=None, description="Sort key.")
    page: str | None = Field(default=None, description="1-based page number.")
    page_size: str | None = Field(default=None, description="Items per page.")


@operation(
    summary="Search the marketplace", tags=["marketplace"], errors=[UpstreamError], request_model=MarketplaceSearchQuery
)
async def marketplace_search(
    q: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    namespace: str | None = None,
    tier: str | None = None,
    contract: str | None = None,
    sort: str | None = None,
    page: str | None = None,
    page_size: str | None = None,
) -> dict[str, Any]:
    """Proxy the registry's public search, forwarding the whitelisted facets.

    ``tags`` is multi-value end to end (the registry receives repeated ``tags``
    params); every other facet is single-valued. ``None`` facets are dropped. The
    registry's rows ride through unchanged, so display metadata (``display_name``,
    ``icon_url``) is transparently forwarded.
    """
    params: dict[str, str | list[str]] = {}
    if q is not None:
        params["q"] = q
    if kind is not None:
        params["kind"] = kind
    if category is not None:
        params["category"] = category
    if tags:
        params["tags"] = tags
    if namespace is not None:
        params["namespace"] = namespace
    if tier is not None:
        params["tier"] = tier
    if contract is not None:
        params["contract"] = contract
    if sort is not None:
        params["sort"] = sort
    if page is not None:
        params["page"] = page
    if page_size is not None:
        params["page_size"] = page_size
    try:
        return await RegistryClient().search(params)
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(summary="Get a marketplace listing's detail", tags=["marketplace"], errors=[NotFoundError, UpstreamError])
async def marketplace_plugin_detail(ns: str, name: str) -> dict[str, Any]:
    """One listing's detail composed with its version rows in a single body, so
    the detail view (listing + the Versions card) is one request. The registry's
    display metadata (``display_name``/``homepage_url``/``license``/``readme_md``)
    survives the spread.
    """
    registry = RegistryClient()
    try:
        listing = await registry.plugin(ns, name)
        versions = await registry.versions(ns, name)
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc
    return {**listing, "versions": versions}


@operation(summary="List marketplace categories", tags=["marketplace"], errors=[UpstreamError])
async def marketplace_categories() -> list[str]:
    """The registry's controlled category vocabulary — a plain array Studio
    renders as facet chips."""
    try:
        return await RegistryClient().categories()
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(summary="List marketplace item kinds", tags=["marketplace"], errors=[UpstreamError])
async def marketplace_kinds() -> list[str]:
    """The registry's controlled item-kind vocabulary — a plain array Studio
    renders as facet chips."""
    try:
        return await RegistryClient().kinds()
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(summary="List installed marketplace plugins", tags=["marketplace"], errors=[UpstreamError, OperationFailed])
async def marketplace_installed() -> dict[str, Any]:
    """The installed inventory + the boot-quarantined plugins, in one body:
    ``{"installed": [...], "quarantined": [{"name", "reason"}, ...]}``.

    Each installed row carries the update picture computed from the registry's
    version rows against the RUNNING contract: ``latest`` (newest published
    version, or ``null`` with none), ``update_available`` (a newer COMPATIBLE
    version exists — an incompatible newer version never advertises as an
    update), ``incompatible_newer`` (the newest such blocked version, so "an
    update exists but needs a newer core" is visible, or ``null``), and
    ``compat`` — the live ``{status, reason}`` verdict of the row's installed
    distribution against the running contract. A per-row not-found — the
    upstream listing vanished or was suspended — is row STATE, not a route
    failure: that row answers ``latest: null``, ``update_available: false``,
    ``missing_upstream: true``, so one dead listing never fails the whole
    inventory. A transport/garbled-upstream failure still surfaces as a 502,
    and a garbled LOCAL row (an unparsable stored version) as a 500 — serving
    rows without their update picture would be a silent degrade of the spec'd
    shape.

    Each row also carries ``items`` — the ``{kind, name}`` of every item its
    stored spec provides (from LOCAL truth, never the registry). Studio joins
    these against the manifest's ``mcp`` entry titles to render an
    installer-written entry read-only.

    Each row also carries ``route_mounts`` — the persisted ``{item_name: base}``
    mount the row is installed at (empty means every item at its declared base),
    so Studio seeds its update flow and renders routes at the ACTUAL mounted base.

    ``quarantined`` mirrors the boot pass's plugin-quarantine registry — the
    plugins this worker SKIPPED (incompatible or import-broken) with the
    human-readable reason each.
    """
    # OFF gate: with no install-attribution store there is no installed inventory —
    # the honest answer is an empty ``installed`` list. The in-process quarantine
    # list is independent of the store and MUST still be served truthfully.
    if not component_store_configured(SKELETON_COMPONENT):
        quarantined = [{"name": name, "reason": reason} for name, reason in sorted(quarantined_plugins().items())]
        return {"installed": [], "quarantined": quarantined}
    registry = RegistryClient()
    contract = running_contract_version()
    rows: list[dict[str, Any]] = []
    for record in await MarketplaceInstallStore().list_installed():
        ns, _, name = record.ref.partition("/")
        missing_upstream = False
        targets: UpdateTargets | None = None
        try:
            versions = await registry.versions(ns, name)
            targets = update_targets(versions, installed_version=record.version, contract_version=contract)
        except ListingNotFoundError:
            missing_upstream = True
        except MarketplaceError as exc:
            raise _to_operation_error(exc) from exc
        package = record.spec.get("package")
        if isinstance(package, str):
            compat = dist_compat(package)
        else:
            # A row whose stored spec names no package distribution cannot be
            # verdicted — surfaced as unknown, never a silent "compatible".
            compat = CompatVerdict("unknown", f"the stored spec for {record.ref} names no package distribution")
        rows.append(
            {
                "ref": record.ref,
                "version": record.version,
                "source": record.source,
                "installed_at": record.installed_at.isoformat(),
                "latest": targets.latest if targets is not None else None,
                "update_available": targets.update_available if targets is not None else False,
                "incompatible_newer": targets.incompatible_newer if targets is not None else None,
                "missing_upstream": missing_upstream,
                "compat": compat.as_payload(),
                "items": _provided_items(record.spec),
                "route_mounts": record.route_mounts,
            }
        )
    quarantined = [{"name": name, "reason": reason} for name, reason in sorted(quarantined_plugins().items())]
    return {"installed": rows, "quarantined": quarantined}


@operation(summary="Get advisories for installed plugins", tags=["marketplace"], errors=[UpstreamError])
async def marketplace_advisories() -> dict[str, Any]:
    """The advisory snapshot for the installed plugins, no older than the
    configured poll interval (a stale snapshot is refreshed on demand, and a
    refresh failure raises a loud 502 rather than serving stale data)."""
    # OFF gate: advisories are computed for the installed inventory; with no store
    # there is nothing installed, so the honest answer is an empty snapshot fetched
    # now (never a Postgres open for an absent inventory).
    if not component_store_configured(SKELETON_COMPONENT):
        return {"advisories": [], "fetched_at": datetime.now(UTC).isoformat()}
    try:
        state = await advisories.current(marketplace_settings().advisories_interval_s)
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc
    return {"advisories": state.advisories, "fetched_at": state.fetched_at.isoformat()}


@operation(
    summary="Install a marketplace plugin",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[
        BadRequestError,
        NotFoundError,
        ConflictError,
        UpstreamError,
        NotSupportedError,
        UnavailableError,
        OperationFailed,
    ],
    request_model=MarketplaceInstall,
)
async def marketplace_install(
    ref: str,
    version: str | None = None,
    env: dict[str, str] | None = None,
    secret_keys: list[str] | None = None,
    route_mounts: dict[str, str] | None = None,
    accept_public_routes: bool = False,
) -> dict[str, Any]:
    """Resolve, pip install, patch the manifest, reload, and record attribution —
    aborting and unwinding on any failure (see :meth:`Installer.install`). For an
    mcp-server install ``env`` / ``secret_keys`` satisfy the entry's required ``!ENV``
    markers in the same combined transaction. ``route_mounts`` remaps declared route
    bases; ``accept_public_routes`` acknowledges public routes. The result's
    ``routes`` lists every route the install mounted."""
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store the
    # install cannot record, so it refuses with a named, machine-readable reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        return await Installer().install(
            ref,
            version,
            env=env,
            secret_keys=secret_keys,
            route_mounts=route_mounts,
            accept_public_routes=accept_public_routes,
        )
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(
    summary="Preview a marketplace install",
    tags=["marketplace"],
    errors=[BadRequestError, NotFoundError, ConflictError, UpstreamError, NotSupportedError],
    request_model=MarketplaceInstallPreview,
)
async def marketplace_install_preview(
    ref: str,
    version: str | None = None,
    route_mounts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a candidate install/update and report its routes WITHOUT changing any
    state: the resolved routes per item with ``route_mounts`` overrides applied, the
    collisions against the live registry (excluding the plugin's own routes on an
    update preview), the public routes requiring acceptance, and the ``new`` public
    rows an update has not already approved (see :meth:`Installer.preview`)."""
    # OFF gate: preview is the install's dry-run; with no attribution store an install
    # cannot proceed, so the preview refuses with the same named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        return await Installer().preview(ref, version, route_mounts=route_mounts)
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(
    summary="Uninstall a marketplace plugin",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[NotFoundError, NotSupportedError, UnavailableError, OperationFailed],
    request_model=MarketplaceUninstall,
)
async def marketplace_uninstall(ref: str) -> dict[str, Any]:
    """Unpatch the manifest, reload, pip uninstall, and drop attribution —
    convergent and registry-free (see :meth:`Installer.uninstall`)."""
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store there
    # is nothing recorded to uninstall, so it refuses with a named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        return await Installer().uninstall(ref)
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(
    summary="Update a marketplace plugin",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[
        BadRequestError,
        NotFoundError,
        ConflictError,
        UpstreamError,
        NotSupportedError,
        UnavailableError,
        OperationFailed,
    ],
    request_model=MarketplaceUpdate,
)
async def marketplace_update(
    ref: str,
    version: str | None = None,
    env: dict[str, str] | None = None,
    secret_keys: list[str] | None = None,
    route_mounts: dict[str, str] | None = None,
    accept_public_routes: bool = False,
) -> dict[str, Any]:
    """Resolve the target, pip upgrade, re-patch the manifest, reload, and upsert
    attribution — with the same pre-flights as install (see :meth:`Installer.update`).
    ``env`` / ``secret_keys`` satisfy a new mcp-server version's required markers.
    ``route_mounts`` remaps declared route bases (surviving items keep their stored
    base); ``accept_public_routes`` acknowledges public routes not already approved.
    The result's ``routes`` lists every route the update mounted."""
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store the
    # update cannot upsert, so it refuses with a named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        return await Installer().update(
            ref,
            version,
            env=env,
            secret_keys=secret_keys,
            route_mounts=route_mounts,
            accept_public_routes=accept_public_routes,
        )
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc


@operation(
    summary="Upgrade all installed marketplace plugins",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[NotSupportedError, UnavailableError],
)
async def marketplace_upgrade_all() -> dict[str, Any]:
    """Move every installed plugin onto its latest COMPATIBLE version in one
    lock-held batch, answering ``{"results": [{ref, outcome, detail}, ...]}``.

    Per-ref failures (including a ref with NO compatible version) are report
    entries, never a batch abort — the ONE operation-level failure is the
    fleet marketplace lock being held elsewhere (the retriable 503). See
    :meth:`Installer.upgrade_all` for the outcome vocabulary.
    """
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store there
    # is no installed inventory to upgrade, so it refuses with a named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        return {"results": await Installer().upgrade_all()}
    except MarketplaceError as exc:
        raise _to_operation_error(exc) from exc
