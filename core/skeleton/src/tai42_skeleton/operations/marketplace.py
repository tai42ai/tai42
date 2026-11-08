"""Marketplace operations — search, browse, and manage installed plugins.

Eleven operations back the ``/api/marketplace/*`` surface: seven reads (search the
registry, one listing's detail with its versions, the category vocabulary, the
item-kind vocabulary, the installed inventory with per-row compat/update
availability, the advisory snapshot, and
a no-side-effect install/update route preview) and four environment-mutating flows
(install, uninstall, update, upgrade-all) driven by
:class:`~tai42_skeleton.marketplace.installer.Installer`.

Every internal :class:`~tai42_skeleton.marketplace.errors.MarketplaceError` an
operation meets is raised as the operation error
:func:`~tai42_skeleton.operations._marketplace_errors.to_operation_error` classifies it to.

install/uninstall/update/upgrade-all mutate the running environment by running
arbitrary third-party code, so each is ``destructive`` and ``authority_changing``
(off the default MCP tool surface — tier 2 — includable only by an explicit
``api_tools.include``) and ``reload_gated`` (the flow ends in a manifest reload,
so the adapter answers a retriable 503 while a reload is in flight).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from tai42_contract.app.responses import OpaqueJson
from tai42_contract.errors import UnavailableError
from tai42_kit.db import component_store_configured

from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.marketplace import advisories
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.compat import (
    CompatVerdict,
    UpdateTargets,
    declared_compat,
    running_contract_version,
    update_targets,
)
from tai42_skeleton.marketplace.errors import (
    ListingNotFoundError,
    LocalStateError,
    MalformedRefError,
    MarketplaceError,
)
from tai42_skeleton.marketplace.installer import Installer
from tai42_skeleton.marketplace.resolve import parse_ref, spec_from_row
from tai42_skeleton.marketplace.settings import marketplace_settings
from tai42_skeleton.marketplace.store import MarketplaceInstallStore
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationFailedError,
    UpstreamError,
    operation,
)
from tai42_skeleton.operations._broadcast import (
    apply_response,
    roll_outcome_body,
    translate_orphan_env_write,
)
from tai42_skeleton.operations._marketplace_errors import to_operation_error
from tai42_skeleton.operations.response_models.marketplace import (
    AdvisorySnapshot,
    InstalledInventory,
    InstallPreview,
    InstallResult,
    UninstallResult,
    UpgradeAllResult,
)
from tai42_skeleton.operations.response_models.shared import StringList

# The machine-readable code every marketplace mutation OFF refusal carries when the
# install-attribution store is unconfigured; the message is rendered from the live
# binding at raise time. Hoisted so the four mutation refusals read one way.
_NOT_CONFIGURED_CODE = "marketplace-not-configured"
_NOT_CONFIGURED_NOUN = "marketplace install store"


class MarketplaceInstall(BaseModel):
    """Install a marketplace plugin by ref, optionally pinning a version.

    ``env`` / ``secret_keys`` are accepted for an install whose spec declares
    install-time env (an mcp entry's required ``!ENV`` markers or an oauth connector's
    client-credential env): these values are written to the env store in the same
    transaction that writes the provides entry (never deferred), and ``secret_keys``
    marks the given keys secret (appended to ``TAI_ENV_SECRET_KEYS``; an oauth
    connector's client secret is masked by derivation with no mark needed). Declared on
    the model so a Studio body carrying them is not silently dropped (pydantic ignores
    unknown fields).

    ``route_mounts`` remaps a route-carrying item's declared base (``{item_name:
    base}``); ``accept_public_routes`` acknowledges that the install's public routes
    answer WITHOUT authentication (required when any public route is declared).
    """

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

    ``env`` / ``secret_keys`` mirror :class:`MarketplaceInstall`: a new version
    adding a required ``!ENV`` marker or connector-env is loudly refused unless its
    value is supplied here. ``route_mounts`` remaps a route-carrying item's base (a
    surviving item with no override keeps its stored base); ``accept_public_routes``
    acknowledges public routes NOT already approved in the installed version.
    """

    ref: str
    version: str | None = None
    env: dict[str, str] | None = None
    secret_keys: list[str] | None = None
    route_mounts: dict[str, str] | None = None
    accept_public_routes: bool = False


class MarketplaceInstallPreview(BaseModel):
    """Preview an install/update by ref, WITHOUT changing state.

    Resolve the target spec and report its routes with any ``route_mounts`` base
    overrides applied.
    """

    ref: str
    version: str | None = None
    route_mounts: dict[str, str] | None = None


class MarketplaceSearchQuery(BaseModel):
    """The marketplace search door's facets: a repeated ``?tags=`` array plus single-valued facets.

    Spec metadata only — the door parses its query at the HTTP edge.
    """

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


def _search_params(
    q: str | None,
    kind: str | None,
    category: str | None,
    tags: list[str] | None,
    namespace: str | None,
    tier: str | None,
    contract: str | None,
    sort: str | None,
    page: str | None,
    page_size: str | None,
) -> dict[str, str | list[str]]:
    """The whitelisted registry-search query.

    The single-valued facets in declared order (dropping ``None``) plus the one
    multi-value facet ``tags`` (kept when truthy). One job: build the forwarded query.
    """
    facets: tuple[tuple[str, str | list[str] | None, bool], ...] = (
        ("q", q, False),
        ("kind", kind, False),
        ("category", category, False),
        ("tags", tags, True),
        ("namespace", namespace, False),
        ("tier", tier, False),
        ("contract", contract, False),
        ("sort", sort, False),
        ("page", page, False),
        ("page_size", page_size, False),
    )
    params: dict[str, str | list[str]] = {}
    for name, value, multi in facets:
        if multi:
            if value:
                params[name] = value
        elif value is not None:
            params[name] = value
    return params


@operation(
    summary="Search the marketplace",
    tags=["marketplace"],
    errors=[UpstreamError],
    request_model=MarketplaceSearchQuery,
    response_model=OpaqueJson,
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
    params = _search_params(q, kind, category, tags, namespace, tier, contract, sort, page, page_size)
    try:
        return await RegistryClient().search(params)
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc


@operation(
    summary="Get a marketplace listing's detail",
    tags=["marketplace"],
    errors=[NotFoundError, UpstreamError],
    response_model=OpaqueJson,
)
async def marketplace_plugin_detail(ns: str, name: str) -> dict[str, Any]:
    """One listing's detail composed with its version rows in a single body.

    So the detail view (listing + the Versions card) is one request. The registry's
    display metadata (``display_name``/``homepage_url``/``license``/``readme_md``)
    survives the spread.
    """
    registry = RegistryClient()
    try:
        listing = await registry.plugin(ns, name)
        versions = await registry.versions(ns, name)
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc
    return {**listing, "versions": versions}


@operation(
    summary="List marketplace categories",
    tags=["marketplace"],
    errors=[UpstreamError],
    response_model=StringList,
)
async def marketplace_categories() -> list[str]:
    """The registry's controlled category vocabulary — a plain array Studio renders as facet chips."""
    try:
        return await RegistryClient().categories()
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc


@operation(
    summary="List marketplace item kinds",
    tags=["marketplace"],
    errors=[UpstreamError],
    response_model=StringList,
)
async def marketplace_kinds() -> list[str]:
    """The registry's controlled item-kind vocabulary — a plain array Studio renders as facet chips."""
    try:
        return await RegistryClient().kinds()
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc


@operation(
    summary="List installed marketplace plugins",
    tags=["marketplace"],
    errors=[UpstreamError, OperationFailedError],
    response_model=InstalledInventory,
)
async def marketplace_installed() -> dict[str, Any]:
    """The installed inventory.

    Shaped ``{"installed": [...]}``.

    Each installed row carries the update picture computed from the registry's
    version rows against the RUNNING contract: ``latest`` (newest published
    version, or ``null`` with none), ``update_available`` (a newer COMPATIBLE
    version exists — an incompatible newer version never advertises as an
    update), ``incompatible_newer`` (the newest such blocked version, so "an
    update exists but needs a newer core" is visible, or ``null``), and
    ``compat`` — the ``{status, reason}`` verdict of the row's declared
    ``tai42-contract`` range against the running contract (a row the plugin layer
    leaves out for that range reads ``incompatible`` with the same reason). A per-row not-found — the
    upstream listing vanished or was suspended — is row STATE, not a route
    failure: that row answers ``latest: null``, ``update_available: false``,
    ``missing_upstream: true``, so one dead listing never fails the whole
    inventory. A transport/garbled-upstream failure still surfaces as a 502,
    and a garbled LOCAL row (a stored spec that no longer validates, a malformed
    stored ref, an unparsable stored version) as a 500 naming the row — serving
    rows without their update picture would be a silent degrade of the spec'd
    shape.

    Each row also carries ``items`` — the ``{kind, name}`` of every item its
    stored spec provides (from LOCAL truth, never the registry). Studio joins
    these against the manifest's ``mcp`` entry titles to render an
    installer-written entry read-only.

    Each row also carries ``route_mounts`` — the persisted ``{item_name: base}``
    mount the row is installed at (empty means every item at its declared base),
    so Studio seeds its update flow and renders routes at the ACTUAL mounted base.

    Each row also carries ``delivery`` — ``package`` for a pip-installed plugin,
    ``descriptor`` for a descriptor-only plugin whose stored spec names no package.
    """
    # OFF gate: with no install-attribution store there is no installed inventory —
    # the honest answer is an empty ``installed`` list.
    if not component_store_configured(SKELETON_COMPONENT):
        return {"installed": []}
    registry = RegistryClient()
    contract = running_contract_version()
    rows: list[dict[str, Any]] = []
    for record in await MarketplaceInstallStore().list_installed():
        try:
            spec = spec_from_row(record)
            try:
                ns, name = parse_ref(record.ref)
            except MalformedRefError as exc:
                raise LocalStateError(f"the stored ref {record.ref!r} is corrupt") from exc
        except LocalStateError as exc:
            raise to_operation_error(exc) from exc
        missing_upstream = False
        targets: UpdateTargets | None = None
        try:
            versions = await registry.versions(ns, name)
            targets = update_targets(versions, installed_version=record.version, contract_version=contract)
        except ListingNotFoundError:
            missing_upstream = True
        except MarketplaceError as exc:
            raise to_operation_error(exc) from exc
        if spec.package is not None:
            compat = declared_compat(spec.package, spec.contract)
        else:
            # A row whose stored spec names no package distribution cannot be
            # verdicted — surfaced as unknown, never a silent "compatible".
            compat = CompatVerdict("unknown", f"the stored spec for {record.ref} names no package distribution")
        rows.append(
            {
                "ref": record.ref,
                "version": record.version,
                "source": record.source,
                "delivery": spec.delivery,
                "installed_at": record.installed_at.isoformat(),
                "latest": targets.latest if targets is not None else None,
                "update_available": targets.update_available if targets is not None else False,
                "incompatible_newer": targets.incompatible_newer if targets is not None else None,
                "missing_upstream": missing_upstream,
                "compat": compat.as_payload(),
                "items": [{"kind": item.kind.value, "name": item.name} for item in spec.provides],
                "route_mounts": record.route_mounts,
            }
        )
    return {"installed": rows}


@operation(
    summary="Get advisories for installed plugins",
    tags=["marketplace"],
    errors=[UpstreamError],
    response_model=AdvisorySnapshot,
)
async def marketplace_advisories() -> dict[str, Any]:
    """The advisory snapshot for the installed plugins, no older than the configured poll interval.

    A stale snapshot is refreshed on demand, and a refresh failure raises a loud 502
    rather than serving stale data.
    """
    # OFF gate: advisories are computed for the installed inventory; with no store
    # there is nothing installed, so the honest answer is an empty snapshot fetched
    # now (never a Postgres open for an absent inventory).
    if not component_store_configured(SKELETON_COMPONENT):
        return {"advisories": [], "fetched_at": datetime.now(UTC).isoformat()}
    try:
        state = await advisories.current(marketplace_settings().advisories_interval_s)
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc
    return {"advisories": state.advisories, "fetched_at": state.fetched_at.isoformat()}


def render_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Render an installer receipt: its ``reload`` (the change's ``ApplyResult``) in the shared reply shape.

    The installer hands the structured outcome up; the op renders it with :func:`apply_response`
    (``None`` — an uninstall that ran no apply — stays ``None``).
    """
    result = receipt["reload"]
    if result is None:
        return receipt
    return {**receipt, "reload": apply_response(result)}


@operation(
    summary="Install a marketplace plugin",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    holds_writer_exit=True,
    authority_changing=True,
    errors=[
        BadRequestError,
        NotFoundError,
        ConflictError,
        UpstreamError,
        NotSupportedError,
        UnavailableError,
        OperationFailedError,
    ],
    request_model=MarketplaceInstall,
    response_model=InstallResult,
)
async def marketplace_install(
    ref: str,
    version: str | None = None,
    env: dict[str, str] | None = None,
    secret_keys: list[str] | None = None,
    route_mounts: dict[str, str] | None = None,
    accept_public_routes: bool = False,
) -> Any:
    """Resolve, pip install, patch the manifest, reload, and record attribution.

    Nothing is pip-installed for a descriptor-only plugin; aborts and unwinds on any
    failure (see :meth:`Installer.install`). When the spec declares install-time env
    ``env`` / ``secret_keys`` satisfy the required ``!ENV`` markers or connector-env in
    the same combined transaction. ``route_mounts`` remaps declared route bases;
    ``accept_public_routes`` acknowledges public routes. The result's ``routes`` lists
    every route the install mounted. An install-time env change carrying a recycle-class key
    rolls a worker recycle (the receipt's ``reload.recycle``); when it converged and this worker
    needs its own restart, it recycles itself after replying.
    """
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store the
    # install cannot record, so it refuses with a named, machine-readable reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    # ``translate_orphan_env_write`` maps a combined env+manifest partial failure (a marks
    # write landed while the manifest persist exhausted retries) to a loud 500 — the same
    # typed error a connector drop through ``manifest replace`` gets — so a connector
    # install/uninstall/update never 500s untyped at the boundary.
    with translate_orphan_env_write():
        try:
            receipt = await Installer().install(
                ref,
                version,
                env=env,
                secret_keys=secret_keys,
                route_mounts=route_mounts,
                accept_public_routes=accept_public_routes,
            )
        except MarketplaceError as exc:
            raise to_operation_error(exc) from exc
    return render_receipt(receipt)


@operation(
    summary="Preview a marketplace install",
    tags=["marketplace"],
    errors=[BadRequestError, NotFoundError, ConflictError, UpstreamError, NotSupportedError],
    request_model=MarketplaceInstallPreview,
    response_model=InstallPreview,
)
async def marketplace_install_preview(
    ref: str,
    version: str | None = None,
    route_mounts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a candidate install/update and report its routes WITHOUT changing any state.

    The resolved routes per item with ``route_mounts`` overrides applied, the
    collisions against the live registry (excluding the plugin's own routes on an
    update preview), the public routes requiring acceptance, and the ``new`` public
    rows an update has not already approved (see :meth:`Installer.preview`).
    """
    # OFF gate: preview is the install's dry-run; with no attribution store an install
    # cannot proceed, so the preview refuses with the same named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        return await Installer().preview(ref, version, route_mounts=route_mounts)
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc


@operation(
    summary="Uninstall a marketplace plugin",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    holds_writer_exit=True,
    authority_changing=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError, UnavailableError, OperationFailedError],
    request_model=MarketplaceUninstall,
    response_model=UninstallResult,
)
async def marketplace_uninstall(ref: str) -> Any:
    """Swap in the plugin environment without the plugin, unpatch the manifest and reload, then drop its record.

    Convergent and registry-free (see :meth:`Installer.uninstall`).
    """
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store there
    # is nothing recorded to uninstall, so it refuses with a named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    with translate_orphan_env_write():
        try:
            receipt = await Installer().uninstall(ref)
        except MarketplaceError as exc:
            raise to_operation_error(exc) from exc
    return render_receipt(receipt)


@operation(
    summary="Update a marketplace plugin",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    holds_writer_exit=True,
    authority_changing=True,
    errors=[
        BadRequestError,
        NotFoundError,
        ConflictError,
        UpstreamError,
        NotSupportedError,
        UnavailableError,
        OperationFailedError,
    ],
    request_model=MarketplaceUpdate,
    response_model=InstallResult,
)
async def marketplace_update(
    ref: str,
    version: str | None = None,
    env: dict[str, str] | None = None,
    secret_keys: list[str] | None = None,
    route_mounts: dict[str, str] | None = None,
    accept_public_routes: bool = False,
) -> Any:
    """Resolve the target, pip upgrade, re-patch the manifest, reload, and upsert attribution.

    Nothing is pip-upgraded for a descriptor-only plugin; the same pre-flights apply
    as install (see :meth:`Installer.update`). ``env`` / ``secret_keys`` satisfy a new
    version's required ``!ENV`` markers or connector-env. ``route_mounts`` remaps
    declared route bases (surviving items keep their stored base);
    ``accept_public_routes`` acknowledges public routes not already approved. The
    result's ``routes`` lists every route the update mounted. An update-time env change
    carrying a recycle-class key rolls a worker recycle (the receipt's ``reload.recycle``);
    when it converged and this worker needs its own restart, it recycles itself after replying.
    """
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store the
    # update cannot upsert, so it refuses with a named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    with translate_orphan_env_write():
        try:
            receipt = await Installer().update(
                ref,
                version,
                env=env,
                secret_keys=secret_keys,
                route_mounts=route_mounts,
                accept_public_routes=accept_public_routes,
            )
        except MarketplaceError as exc:
            raise to_operation_error(exc) from exc
    return render_receipt(receipt)


@operation(
    summary="Upgrade all installed marketplace plugins",
    tags=["marketplace"],
    destructive=True,
    reload_gated=True,
    holds_writer_exit=True,
    authority_changing=True,
    errors=[NotSupportedError, UnavailableError, OperationFailedError],
    response_model=UpgradeAllResult,
)
async def marketplace_upgrade_all() -> Any:
    """Move every installed plugin onto its latest COMPATIBLE version in one lock-held batch.

    Answers ``{"results": [{ref, outcome, detail, span_release_error}, ...], "recycle", "awaiting_respawn",
    "roll_deferred", "roll_deferred_to"}``;
    a row's ``span_release_error`` names the failed config-store span releases of the config writes
    its ref landed — an upgrade's write, or a failed upgrade's forward and restore writes — joined in
    write order (``null`` when each released or the ref wrote nothing). ``recycle`` is the one fleet
    recycle roll the batch's reloads settled once it ended (``null`` when the roll had no
    worker to visit — every worker's reload confirmed no recycle-class key, or the only ones naming
    one were refused at their epoch build or are the writer whose own restart cannot follow as its exit —
    or the batch reloaded nothing) and ``awaiting_respawn`` the workers still awaiting a
    respawn after it; when the roll converged, this worker's own restart, when it needs one,
    follows as its exit once the reply is sent. ``roll_deferred`` is ``true`` when the roll was handed to the worker
    ``roll_deferred_to`` names, which carries it out after the reply.

    Per-ref failures (including a ref with NO compatible version) are report
    entries, never a batch abort. The operation-level failures are the fleet
    marketplace lock being held elsewhere (the retriable 503) and the batch's
    landed config changes whose fleet recycle roll did not complete (a 500 saying
    so). See :meth:`Installer.upgrade_all` for the outcome vocabulary.
    """
    # OFF gate — BEFORE the fleet PG advisory lock: with no attribution store there
    # is no installed inventory to upgrade, so it refuses with a named reason.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    try:
        report = await Installer().upgrade_all()
    except MarketplaceError as exc:
        raise to_operation_error(exc) from exc
    return {"results": report.results, **roll_outcome_body(report.roll)}
