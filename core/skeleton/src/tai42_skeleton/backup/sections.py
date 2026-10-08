"""The host's own backup sections — the skeleton as first consumer of its ``app.backup`` facet.

Each section is a thin exporter/importer pair over the owning subsystem's existing
read/write seam; no backup logic lives in the subsystems. Exporters/importers reach
the live subsystems through ``tai42_app`` at CALL time, so a section is a closure
over the running app, not a snapshot taken at registration.

An exporter returns a JSON-safe payload; an importer returns a
:class:`BackupSectionReport`. The platform reads only its typed fields
(``created``/``updated``/``skipped``/``errors`` plus the ``fanout`` ``manifest`` and
``env`` fill with the per-worker fleet report of their reload broadcast); each
section's own counts (the ``skipped_existing`` tally, ``access_control``'s minted
``new_api_keys``) ride the open ``details`` map. A section whose backing subsystem is
absent lets the seam raise; the router catches it per-section — these functions never
swallow it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.app.responses import FanoutSummary
from tai42_contract.backup import BackupSectionReport
from tai42_contract.states import StateBinding
from tai42_contract.states.errors import StatesError
from tai42_contract.template import TemplatedText
from tai42_kit.utils.schedule_subject import SCHEDULE_STATE_BINDING_ARG

from tai42_skeleton.backup.registry import current_import_mode
from tai42_skeleton.schedules import check_schedule_definition
from tai42_skeleton.tools.state_binding import is_binding_refusal

logger = logging.getLogger(__name__)


def _empty_report() -> BackupSectionReport:
    """A zeroed report for a mode-aware section, its ``details`` carrying a ``skipped_existing`` tally.

    ``skipped`` counts per-record REJECTIONS (invalid/unauthorized, each also in
    ``errors``); ``details["skipped_existing"]`` counts records left untouched under
    ``skip`` mode — a clean skip, never an error.
    """
    return BackupSectionReport(details={"skipped_existing": 0})


# -- manifest ----------------------------------------------------------------


def _export_manifest() -> dict[str, Any]:
    # Preserved-tag view: each ``!ENV`` reference travels as its literal marker
    # string, not the resolved secret, so this non-secret section carries no live secret.
    return tai42_app.config.config_manager.read_manifest_preserved()


async def _import_manifest(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.config.service import ConfigService
    from tai42_skeleton.operations._broadcast import translate_orphan_env_write

    # Replaces the persisted manifest as a whole through the pipeline (validate on the
    # resolved projection, persist, reload, broadcast). Validation failure raises here
    # with nothing persisted; the router records it as this section's error. When the
    # replacement DROPS an oauth connector the replace crosses the combined env+manifest
    # seam (to keep the leaving secret masked), so a manifest-persist partial failure is
    # mapped to a loud, typed OperationFailedError exactly as the marketplace / manifest doors.
    with translate_orphan_env_write():
        result = await ConfigService.from_app().apply_replace(payload)
    return BackupSectionReport(updated=1, fanout=FanoutSummary.model_validate(result.fanout))


# -- env ---------------------------------------------------------------------


def _export_env() -> dict[str, str]:
    try:
        return tai42_app.config.config_manager.read_env()
    except FileNotFoundError:
        # No env file yet is a normal empty state, not an error.
        return {}


async def _import_env(payload: dict[str, str]) -> BackupSectionReport:
    from tai42_skeleton.config.service import ConfigService

    config_manager = tai42_app.config.config_manager
    try:
        existing = config_manager.read_env()
    except FileNotFoundError:
        existing = {}
    created = sum(1 for key in payload if key not in existing)
    updated = sum(1 for key in payload if key in existing)
    # Apply through the pipeline: validated, reloaded, broadcast to the fleet.
    result = await ConfigService.from_app().apply_env_change(payload)
    return BackupSectionReport(created=created, updated=updated, fanout=FanoutSummary.model_validate(result.fanout))


# -- access_control ----------------------------------------------------------


async def _export_access_control() -> dict[str, Any]:
    from tai42_skeleton.access_control import management

    # Principals travel WITH their own policy row (the fingerprint-less role policy), so a
    # restore recreates the identity roster before re-minting the keys that belong to it.
    principals: list[dict[str, Any]] = []
    for principal in await management.list_principals():
        policy = await management.get_policy_body(principal["user_id"])
        created_at = principal.get("created_at")
        principals.append(
            {
                "user_id": principal["user_id"],
                "kind": principal["kind"],
                "display_name": principal["display_name"],
                "created_by": principal["created_by"],
                "disabled": principal["disabled"],
                "created_at": created_at.isoformat() if created_at is not None else None,
                "policy": policy or {"scopes": [], "policy_data": {}, "condition": None},
            }
        )

    return {
        # EVERY route mapping url -> value, public routes included (value
        # ``public_resource_id``), not the non-public-only ``get_all_existing_scopes``:
        # a public route must not restore as protected.
        "scopes": await management.get_all_route_mappings(),
        "patterns": await management.get_all_existing_patterns(),
        "principals": principals,
        "tokens": await management.get_all_existing_tokens_payload(),
    }


async def _restore_principals(payload: dict[str, Any], report: BackupSectionReport) -> None:
    """Restore principals FIRST so the token restore below finds every owner provisioned.

    Each principal's own policy row (the fingerprint-less role policy) travels with it. An
    existing principal is identity and is never overwritten; a missing/empty id or a store
    failure is a loud per-principal error.

    A principal whose policy row already exists in Postgres (but whose principal row does
    not — a Redis-only loss recovered from a surviving Postgres) keeps the live policy: the
    policy rows in Postgres are the source of truth, matching the token restore, so the
    principal row is created and the exported policy body is not re-written.
    """
    from tai42_skeleton.access_control import management
    from tai42_skeleton.access_control.store import access_control_store

    store = access_control_store()
    for principal in payload.get("principals") or []:
        user_id = principal.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            report.errors.append(f"principal with missing or empty user_id: {user_id!r}")
            report.skipped += 1
            continue
        if await store.get_principal(user_id) is not None:
            report.details["skipped_existing"] += 1
            continue
        try:
            await store.create_principal(
                user_id, principal["kind"], principal["display_name"], principal.get("created_by")
            )
            if await store.get_policy_body(user_id) is None:
                policy = principal.get("policy") or {"scopes": [], "policy_data": {}, "condition": None}
                await store.create_policy(
                    user_id, list(policy.get("scopes") or []), policy.get("policy_data"), policy.get("condition")
                )
            if principal.get("disabled"):
                await store.set_principal_disabled(user_id, True)
            await management.record_policy_change()
        except (ValueError, KeyError) as exc:
            report.errors.append(f"principal {user_id!r}: {exc}")
            report.skipped += 1
            continue
        report.created += 1


def _canonical_archive_urls(urls: Iterable[str]) -> dict[str, str]:
    """Each archive route url mapped to its canonical form, refusing two urls with one canonical form.

    The route table holds canonical urls, so two archive rows that reduce to one url would
    overwrite each other — the archive is refused before the first write rather than restored
    with a row silently dropped. A url with no canonical form raises its ``MalformedPathError``.
    """
    from tai42_skeleton.access_control.path_canon import canonicalize_path

    canonical: dict[str, str] = {}
    seen: dict[str, str] = {}
    for url in urls:
        form = canonicalize_path(url)
        if form in seen:
            raise ValueError(
                f"access_control archive maps {seen[form]!r} and {url!r} to one canonical route {form!r}; "
                "refusing to restore"
            )
        seen[form] = url
        canonical[url] = form
    return canonical


async def _import_access_control(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.access_control import management
    from tai42_skeleton.access_control.settings import access_control_settings

    mode = current_import_mode()
    report = _empty_report()
    report.details["new_api_keys"] = []
    patterns = payload.get("patterns") or {}
    scopes = payload.get("scopes") or {}
    canonical = _canonical_archive_urls(scopes)

    # Every write below records its change; the policy cache is invalidated once at the end,
    # and still when a write part-way raises.
    async with management.policy_write_batch():
        await _restore_principals(payload, report)

        # Replay route -> scope mappings first so the token restore below finds every
        # referenced scope already provisioned. Keyed by the canonical url the table holds:
        # under ``skip`` an already-mapped url is left as it stands.
        marker = access_control_settings().public_resource_id
        # Read live mappings only when there are scopes to place; a token-only restore needs no store hit.
        existing_urls = set((await management.get_all_route_mappings()).keys()) if scopes else set()
        for url, scope_id in scopes.items():
            existed = canonical[url] in existing_urls
            if existed and mode == "skip":
                report.details["skipped_existing"] += 1
                continue
            if scope_id == marker:
                # The marker is a column value, not a scope: a public route restores through
                # the dedicated pin writer, never ``add_url_to_scope``.
                await management.pin_route_public(url, patterns.get(url))
            else:
                await management.add_url_to_scope(scope_id, url, patterns.get(url))
            if existed:
                report.updated += 1
            else:
                report.created += 1

        await _restore_tokens(payload, report)
    return report


async def _restore_token(token: dict[str, Any], report: BackupSectionReport) -> None:
    """Restore one exported api-key token, minting a fresh key and recording its plaintext.

    A user id with a live key or an account row is a clean ``skipped_existing``; an orphaned
    policy row (its identity record gone) is re-minted onto the surviving policy; a user id
    with no policy row is minted fresh with the owner threaded from the token's management
    home. A token with no owner claim is an ownerless key row — a loud per-token error,
    never re-minted ownerless.
    """
    from tai42_contract.access_control import OWNER_USER_ID_CLAIM

    from tai42_skeleton.access_control import management

    user_id = token.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        report.errors.append(f"token with missing or empty user_id: {user_id!r}")
        report.skipped += 1
        return
    description = token.get("description", "")
    state = await management.api_key_state(user_id)
    if state in ("live", "account"):
        report.details["skipped_existing"] += 1
        return
    owner_user_id = (token.get("policy_data") or {}).get(OWNER_USER_ID_CLAIM)
    if state != "orphaned" and not (isinstance(owner_user_id, str) and owner_user_id):
        # A minted key with no owner claim is an ownerless key row — a loud per-token
        # error, never re-minted ownerless.
        report.errors.append(f"token {user_id!r}: no owner claim (ownerless key; re-initialize the deployment)")
        report.skipped += 1
        return
    try:
        if state == "orphaned":
            api_key = await management.remint_orphaned_api_key(user_id, description)
        else:
            assert isinstance(owner_user_id, str)  # noqa: S101 — narrowed by the guard above
            stored_condition = token.get("condition")
            # Parse the stored templated-text document back through the contract so a
            # malformed one raises here, never restores as a silently dropped condition.
            condition = TemplatedText.model_validate(stored_condition) if stored_condition is not None else None
            api_key, _committed_body, _fingerprint = await management.add_user_api_key(
                user_id,
                description,
                token.get("scopes") or [],
                token.get("policy_data"),
                condition,
                owner_user_id=owner_user_id,
            )
    except ValueError as exc:
        # Per-token failure (collided id, absent scope, bad condition) surfaced loudly.
        report.errors.append(f"token {user_id!r}: {exc}")
        report.skipped += 1
        return
    report.created += 1
    report.details["new_api_keys"].append({"user_id": user_id, "description": description, "api_key": api_key})


async def _restore_tokens(payload: dict[str, Any], report: BackupSectionReport) -> None:
    """Restore every exported api-key token (see :func:`_restore_token`).

    API-key hashes are one-way, so a restore mints BRAND-NEW keys and surfaces each
    plaintext in ``new_api_keys``.
    """
    for token in payload.get("tokens") or []:
        await _restore_token(token, report)


# -- sub_mcp -----------------------------------------------------------------


async def _export_sub_mcp() -> dict[str, Any]:
    # From the durable store (source of truth), not this worker's in-process cache.
    from tai42_skeleton.sub_mcp.store import get_sub_mcp_store

    routes = await get_sub_mcp_store().list_routes()
    return {slug: config.model_dump() for slug, config in routes.items()}


async def _import_sub_mcp(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.sub_mcp import service
    from tai42_skeleton.sub_mcp.store import get_sub_mcp_store

    store = get_sub_mcp_store()
    mode = current_import_mode()
    report = _empty_report()
    for slug, config in payload.items():
        # Counts reflect DURABLE state (store presence), not this worker's local cache.
        existed = await store.get_route(slug) is not None
        if existed and mode == "skip":
            # Existing route (keyed by slug) left untouched — stored config and local binding intact.
            report.details["skipped_existing"] += 1
            continue
        if not isinstance(config, dict) or "tools" not in config:
            # Malformed entry is a loud per-slug rejection, never an aborted restore.
            report.errors.append(
                f"sub-MCP app {slug!r}: malformed backup entry (expected a mapping with a 'tools' key)"
            )
            report.skipped += 1
            continue
        try:
            # The service validates slug shape + transport BEFORE its store write, so a
            # malformed slug is a per-slug rejection, never a phantom route or garbage row.
            await service.register_sub_mcp_app(slug, config["tools"], config.get("transport", "http"))
        except ValueError as exc:
            report.errors.append(f"sub-MCP app {slug!r}: {exc}")
            report.skipped += 1
            continue
        if existed:
            report.updated += 1
        else:
            report.created += 1
    return report


# -- conversations -----------------------------------------------------------


async def _export_conversations() -> dict[str, Any]:
    from tai42_skeleton.conversations.backup import export_conversation_routes

    return await export_conversation_routes()


async def _import_conversations(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.conversations.backup import import_conversation_routes

    return await import_conversation_routes(payload, current_import_mode())


# -- conversation target config ----------------------------------------------


async def _export_conversation_target_config() -> dict[str, Any]:
    from tai42_skeleton.conversations.target_config_backup import export_target_configs

    return await export_target_configs()


async def _import_conversation_target_config(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.conversations.target_config_backup import import_target_configs

    return await import_target_configs(payload, current_import_mode())


# -- templates ---------------------------------------------------------------


async def _export_templates() -> dict[str, str]:
    resource_manager = tai42_app.storage.resource_manager
    paths = await resource_manager.list_resources()
    return {path: await resource_manager.fetch_template(path) for path in paths}


async def _import_templates(payload: dict[str, str]) -> BackupSectionReport:
    from tai42_contract.storage import StoragePathConflictError

    from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path

    resource_manager = tai42_app.storage.resource_manager
    existing = set(await resource_manager.list_resources())
    mode = current_import_mode()
    report = _empty_report()
    for path, content in payload.items():
        try:
            # An untrusted backup can carry a traversal key writing outside the store
            # root; guard each key BEFORE upload. A bad key is a per-path rejection, never
            # a silent drop and never an aborted restore.
            safe_template_path(path)
        except UnsafeTemplatePathError as exc:
            report.errors.append(f"template {path!r}: {exc}")
            report.skipped += 1
            logger.warning("backup restore skipped unsafe template path %r: %s", path, exc)
            continue
        if path in existing and mode == "skip":
            # Existing template (keyed by path) left untouched.
            report.details["skipped_existing"] += 1
            continue
        try:
            await resource_manager.upload_template(path, content)
        except StoragePathConflictError as exc:
            # A key colliding with a directory that still holds templates is a
            # per-path rejection recorded loudly, never a silently dropped or
            # whole-restore-aborting failure.
            report.errors.append(f"template {path!r}: {exc}")
            report.skipped += 1
            logger.warning("backup restore skipped conflicting template path %r: %s", path, exc)
            continue
        if path in existing:
            report.updated += 1
        else:
            report.created += 1
    return report


# -- schedules ---------------------------------------------------------------


async def _export_schedules() -> Any:
    # Opaque: the backend owns the document shape. An unbound backend has no export
    # tool, so run_tool raises the unknown-tool error and the router records it per-section.
    return await tai42_app.tools.run_tool("backend_export_schedules", {})


async def _check_schedule_rows(payload: Any) -> tuple[Any, list[int], list[dict[str, Any]]]:
    """Run the schedule definition check over every row carrying a door binding.

    Returns the rows to forward to the backend, each forwarded row's index in ``payload``, and
    one ``{"index", "name", "error"}`` record per refused row. A row reads as ``{"name",
    "kwargs"}`` with its binding under the reserved kwargs key, the shape every scheduling
    backend exports; a row of any other shape carries no binding the platform can read and is
    forwarded untouched for the backend to judge. A refusal (a malformed binding, a binding the
    states store refuses) keeps the row from the backend; any other error fails the section.
    """
    if not isinstance(payload, list):
        return payload, [], []
    forwarded: list[Any] = []
    positions: list[int] = []
    refused: list[dict[str, Any]] = []
    for index, row in enumerate(payload):
        kwargs = row.get("kwargs") if isinstance(row, dict) else None
        raw = kwargs.get(SCHEDULE_STATE_BINDING_ARG) if isinstance(kwargs, dict) else None
        if raw is not None:
            try:
                await check_schedule_definition(StateBinding.model_validate(raw))
            except ValueError as exc:
                refused.append({"index": index, "name": row.get("name"), "error": str(exc)})
                continue
            except StatesError as exc:
                if not is_binding_refusal(exc):
                    raise
                refused.append({"index": index, "name": row.get("name"), "error": str(exc)})
                continue
        forwarded.append(row)
        positions.append(index)
    return forwarded, positions, refused


async def _import_schedules(payload: Any) -> BackupSectionReport:
    # The document is opaque, but the mode is forwarded explicitly across the tool
    # boundary (a backend cannot read this process's import-mode context). A backend
    # whose import tool predates ``mode`` rejects it — a loud per-section error, not a
    # silent mode mismatch.
    forwarded, positions, refused = await _check_schedule_rows(payload)
    raw = await tai42_app.tools.run_tool(
        "backend_import_schedules", {"schedules": forwarded, "mode": current_import_mode()}
    )
    report = _schedules_report(raw, positions)
    report.errors = [_schedule_error_text(entry) for entry in refused] + report.errors
    report.skipped += len(refused)
    return report


def _schedules_report(raw: Any, positions: list[int]) -> BackupSectionReport:
    """Map the scheduling backend's import result into the typed section report.

    The ``backend_import_schedules`` result is the schedules section's own round-trip
    convention with any task backend: its counts fill the typed fields, its per-row
    error records (each rendered as a string) fill ``errors``, and every other count it
    carries (a backend's ``skipped_existing`` tally, say) rides ``details``. A result
    that is not a mapping is refused loudly rather than mis-reported as an empty restore.
    ``positions`` maps the backend's row index (over the rows it was handed) back to the
    row's index in the restored document.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"schedules backend returned a non-mapping import report: {type(raw).__name__}")
    typed_keys = ("created", "updated", "skipped", "errors")
    errors = [_schedule_error_text(_document_row(entry, positions)) for entry in raw.get("errors") or []]
    details = {key: value for key, value in raw.items() if key not in typed_keys}
    return BackupSectionReport(
        created=int(raw.get("created", 0)),
        updated=int(raw.get("updated", 0)),
        skipped=int(raw.get("skipped", 0)),
        errors=errors,
        details=details,
    )


def _document_row(entry: Any, positions: list[int]) -> Any:
    """A backend error record re-indexed from the backend's row to the document's row.

    Only a record carrying an in-range integer ``index`` is re-indexed; any other shape is kept
    as the backend reported it.
    """
    if isinstance(entry, dict):
        index = entry.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(positions):
            return {**entry, "index": positions[index]}
    return entry


def _schedule_error_text(entry: Any) -> str:
    """Render one backend error record as a string for the typed report.

    A backend reports a per-row error as ``{"index", "name", "error"}``; a plain string
    is kept as-is, so a backend reporting either shape is carried without loss.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        name, index, message = entry.get("name"), entry.get("index"), entry.get("error")
        return f"schedule {name!r} (row {index}): {message}"
    return str(entry)


# -- connector_categories / connector_connections ----------------------------


async def _export_connector_categories() -> dict[str, Any]:
    from tai42_skeleton.connectors.store.backup import export_connector_categories

    return await export_connector_categories()


async def _import_connector_categories(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.connectors.store.backup import import_connector_categories

    return await import_connector_categories(payload, current_import_mode())


async def _export_connector_connections() -> list[dict[str, Any]]:
    from tai42_skeleton.connectors.store.backup import export_connector_connections

    return await export_connector_connections()


async def _import_connector_connections(payload: list[dict[str, Any]]) -> BackupSectionReport:
    from tai42_skeleton.connectors.store.backup import import_connector_connections

    return await import_connector_connections(payload, current_import_mode())


# -- versioned_documents (the kind-agnostic versioned-document store) ---------


async def _export_versioned_documents() -> dict[str, Any]:
    from tai42_skeleton.versioning.backup import export_versioned_documents

    return await export_versioned_documents()


async def _import_versioned_documents(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.versioning.backup import import_versioned_documents

    return await import_versioned_documents(payload, current_import_mode())


# -- tool_meta (the folder tree + per-tool organizational overlay) ------------


async def _export_tool_meta() -> dict[str, Any]:
    from tai42_skeleton.tool_meta.backup import export_tool_meta

    return await export_tool_meta()


async def _import_tool_meta(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.tool_meta.backup import import_tool_meta

    return await import_tool_meta(payload, current_import_mode())


# -- registration ------------------------------------------------------------


def register_core_sections(registry: Any) -> None:
    """Register the skeleton's built-in sections on ``registry``.

    Called once per app-object construction, so an in-place reload (same app object)
    never re-registers. Registration order IS an import's replay order: a section
    deciding its records against live state another section restores is registered AFTER it.
    """
    # Imported here (not at module top) so ``webhooks_section`` can reach this module's
    # ``_empty_report`` without an import cycle.
    from tai42_skeleton.backup.webhooks_section import _export_webhooks, _import_webhooks
    from tai42_skeleton.states.backup import register_states_backup_section

    registry.register_section("manifest", _export_manifest, _import_manifest)
    registry.register_section("env", _export_env, _import_env, secret=True)
    registry.register_section("access_control", _export_access_control, _import_access_control, secret=True)
    registry.register_section("sub_mcp", _export_sub_mcp, _import_sub_mcp)
    # Before ``webhooks``/``conversations``: their token-free scan renders policy
    # conditions naming a stored resource by id, which are templates this section restores.
    registry.register_section("templates", _export_templates, _import_templates)
    # States before every section that restores a binding: a restored binding attaches templates
    # on states, so the declarations and templates it names restore first.
    register_states_backup_section(registry)
    # AFTER ``access_control`` and ``templates`` (records decided against the live policy
    # store, which those restore). secret=True: the bulk export aggregates hook
    # ``tool_kwargs`` and full token hashes, broader than the grantable per-record read.
    registry.register_section("webhooks", _export_webhooks, _import_webhooks, secret=True)
    # AFTER ``access_control``/``templates``, as ``webhooks``. secret=False: export equals the
    # grantable route-list read (``callback_secret`` excluded, re-minted only on overwrite).
    registry.register_section("conversations", _export_conversations, _import_conversations)
    # The per-target conversation config (multichannel opt-in + first-contact greeting).
    # Non-secret operator config — the connectors split's non-secret half — registered
    # beside the routing rows it configures.
    registry.register_section(
        "conversation_target_config",
        _export_conversation_target_config,
        _import_conversation_target_config,
    )
    # Unconditional: an unbound scheduling backend surfaces as a per-section error, not a gap.
    registry.register_section("schedules", _export_schedules, _import_schedules, secret=True)
    registry.register_section("connector_categories", _export_connector_categories, _import_connector_categories)
    registry.register_section(
        "connector_connections", _export_connector_connections, _import_connector_connections, secret=True
    )
    # Body-opaque and kind-agnostic, so ONE section covers every kind. secret=True:
    # the ``settings_profile`` kind stores env values verbatim, secrets included
    # (``SettingsProfileBody.env``), so the section is secret-bearing.
    registry.register_section(
        "versioned_documents", _export_versioned_documents, _import_versioned_documents, secret=True
    )
    # Tool-metadata overlay (folders + per-tool rows). Not secret-bearing.
    registry.register_section("tool_meta", _export_tool_meta, _import_tool_meta)
