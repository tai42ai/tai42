"""The host's own backup sections — the skeleton as first consumer of its
``app.backup`` facet.

Each section is a thin exporter/importer pair over the owning subsystem's existing
read/write seam; no backup logic lives in the subsystems. Exporters/importers reach
the live subsystems through ``tai42_app`` at CALL time, so a section is a closure
over the running app, not a snapshot taken at registration.

An exporter returns a JSON-safe payload; an importer returns a section report
``{"created", "updated", "skipped", "skipped_existing", "errors"}`` (``access_control``
adds ``"new_api_keys"``; ``manifest`` and ``env`` add ``"fanout"``, the per-origin fleet
report of their reload broadcast). A section whose backing subsystem is absent lets
the seam raise; the router catches it per-section — these functions never swallow it.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from tai42_contract.app import tai42_app

from tai42_skeleton.backup.registry import current_import_mode

logger = logging.getLogger(__name__)

# ``skipped`` counts per-record REJECTIONS (invalid/unauthorized, each also in
# ``errors``); ``skipped_existing`` counts records left untouched under ``skip`` mode —
# a clean skip, never an error.
_SectionReport = dict[str, Any]


def _empty_report() -> _SectionReport:
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


# -- manifest ----------------------------------------------------------------


def _export_manifest() -> dict[str, Any]:
    # Preserved-tag view: each ``!ENV`` reference travels as its literal marker
    # string, not the resolved secret, so this non-secret section carries no live secret.
    return tai42_app.config.config_manager.read_manifest_preserved()


async def _import_manifest(payload: dict[str, Any]) -> _SectionReport:
    from tai42_skeleton.config.service import ConfigService

    # Replaces the persisted manifest as a whole through the pipeline (validate on the
    # resolved projection, persist, reload, broadcast). Validation failure raises here
    # with nothing persisted; the router records it as this section's error.
    result = await ConfigService.from_app().apply_replace(payload)
    report = _empty_report()
    report["updated"] = 1
    report["fanout"] = result.fanout
    return report


# -- env ---------------------------------------------------------------------


def _export_env() -> dict[str, str]:
    try:
        return tai42_app.config.config_manager.read_env()
    except FileNotFoundError:
        # No env file yet is a normal empty state, not an error.
        return {}


async def _import_env(payload: dict[str, str]) -> _SectionReport:
    from tai42_skeleton.config.service import ConfigService

    config_manager = tai42_app.config.config_manager
    try:
        existing = config_manager.read_env()
    except FileNotFoundError:
        existing = {}
    report = _empty_report()
    report["created"] = sum(1 for key in payload if key not in existing)
    report["updated"] = sum(1 for key in payload if key in existing)
    # Apply through the pipeline: validated, reloaded, broadcast to the fleet.
    result = await ConfigService.from_app().apply_env_change(payload)
    report["fanout"] = result.fanout
    return report


# -- access_control ----------------------------------------------------------


async def _export_access_control() -> dict[str, Any]:
    from tai42_skeleton.access_control import management

    return {
        # EVERY route mapping url -> value, public routes included (value
        # ``public_resource_id``), not the non-public-only ``get_all_existing_scopes``:
        # a public route must not restore as protected.
        "scopes": await management.get_all_route_mappings(),
        "patterns": await management.get_all_existing_patterns(),
        "tokens": await management.get_all_existing_tokens_payload(),
    }


async def _import_access_control(payload: dict[str, Any]) -> _SectionReport:
    from tai42_skeleton.access_control import management
    from tai42_skeleton.access_control.settings import access_control_settings

    mode = current_import_mode()
    report = _empty_report()
    report["new_api_keys"] = []

    # Replay route -> scope mappings first so the token restore below finds every
    # referenced scope already provisioned. Keyed by ``url``: under ``skip`` an
    # already-mapped url is left as it stands.
    marker = access_control_settings().public_resource_id
    patterns = payload.get("patterns") or {}
    scopes = payload.get("scopes") or {}
    # Read live mappings only when there are scopes to place; a token-only restore needs no store hit.
    existing_urls = set((await management.get_all_route_mappings()).keys()) if scopes else set()
    for url, scope_id in scopes.items():
        existed = url in existing_urls
        if existed and mode == "skip":
            report["skipped_existing"] += 1
            continue
        if scope_id == marker:
            # The marker is a column value, not a scope: a public route restores through
            # the dedicated pin writer, never ``add_url_to_scope``.
            await management.pin_route_public(url, patterns.get(url))
        else:
            await management.add_url_to_scope(scope_id, url, patterns.get(url))
        if existed:
            report["updated"] += 1
        else:
            report["created"] += 1

    # API-key hashes are one-way: a restore mints BRAND-NEW keys and surfaces each
    # plaintext in ``new_api_keys``. Tokens are skip-only under EVERY mode (never
    # overwritten), so an existing user id is a clean ``skipped_existing``, not an error.
    for token in payload.get("tokens") or []:
        user_id = token.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            # Missing/empty user id is a loud per-token rejection, never a record under a blank id.
            report["errors"].append(f"token with missing or empty user_id: {user_id!r}")
            report["skipped"] += 1
            continue
        if await management.get_policy_body(user_id) is not None:
            # Already provisioned: leave the live key in place (revoke then re-import to replace).
            report["skipped_existing"] += 1
            continue
        description = token.get("description", "")
        try:
            api_key, _committed_body, _fingerprint = await management.add_user_api_key(
                user_id,
                description,
                token.get("scopes") or [],
                token.get("policy_data"),
                token.get("condition"),
                token.get("condition_id"),
                token.get("condition_kwargs"),
            )
        except ValueError as exc:
            # Per-token failure (collided id, absent scope) surfaced loudly; the rest still restore.
            report["errors"].append(f"token {user_id!r}: {exc}")
            report["skipped"] += 1
            continue
        report["created"] += 1
        report["new_api_keys"].append({"user_id": user_id, "description": description, "api_key": api_key})

    await management.bump_policy_version()
    return report


# -- sub_mcp -----------------------------------------------------------------


async def _export_sub_mcp() -> dict[str, Any]:
    # From the durable store (source of truth), not this worker's in-process cache.
    from tai42_skeleton.sub_mcp.store import get_sub_mcp_store

    routes = await get_sub_mcp_store().list_routes()
    return {slug: config.model_dump() for slug, config in routes.items()}


async def _import_sub_mcp(payload: dict[str, Any]) -> _SectionReport:
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
            report["skipped_existing"] += 1
            continue
        if not isinstance(config, dict) or "tools" not in config:
            # Malformed entry is a loud per-slug rejection, never an aborted restore.
            report["errors"].append(
                f"sub-MCP app {slug!r}: malformed backup entry (expected a mapping with a 'tools' key)"
            )
            report["skipped"] += 1
            continue
        try:
            # The service validates slug shape + transport BEFORE its store write, so a
            # malformed slug is a per-slug rejection, never a phantom route or garbage row.
            await service.register_sub_mcp_app(slug, config["tools"], config.get("transport", "http"))
        except ValueError as exc:
            report["errors"].append(f"sub-MCP app {slug!r}: {exc}")
            report["skipped"] += 1
            continue
        if existed:
            report["updated"] += 1
        else:
            report["created"] += 1
    return report


# -- webhooks ----------------------------------------------------------------


async def _export_webhooks() -> dict[str, Any]:
    from tai42_skeleton.hooks.cache import get_hooks_manager
    from tai42_skeleton.hooks.trigger_links import export_trigger_links

    manager = get_hooks_manager()
    hooks = await manager.list_hooks()
    # Envelope carries hooks + per-topic verifier bindings + trigger-link records (hashes
    # and metadata only, never a raw token). Bindings must travel with the hooks, or a
    # verified topic restores as a public door.
    return {
        "hooks": [params.model_dump(mode="json") for params in hooks.values()],
        "topic_verifiers": await manager.all_topic_verifiers(),
        **(await export_trigger_links()),
    }


async def _import_webhooks(payload: list[dict[str, Any]] | dict[str, Any]) -> _SectionReport:
    from tai42_contract.hooks import HookParams

    from tai42_skeleton.authz.execution import ExecutionKeyAuthorityError, ExecutionKeyScan
    from tai42_skeleton.authz.token_free import TokenFreeConditionError
    from tai42_skeleton.hooks.cache import get_hooks_manager
    from tai42_skeleton.hooks.trigger_links import (
        TriggerLinkError,
        bound_hashes_by_name,
        restore_tombstone,
        restore_trigger_link,
    )

    manager = get_hooks_manager()
    mode = current_import_mode()
    report = _empty_report()
    # One scan for the whole restore: each distinct execution key is read and rendered once.
    scan = ExecutionKeyScan()

    async def _restore_hooks(hooks: list[dict[str, Any]], unlocked_topics: frozenset[str] = frozenset()) -> None:
        existing = await manager.list_hooks()
        for item in hooks:
            name = item.get("name") if isinstance(item, dict) else None
            try:
                params = HookParams.model_validate(item)
            except ValidationError as exc:
                # Per-hook rejection, never a hook without a bounded identity nor an aborted restore.
                report["errors"].append(f"hook {name!r}: {exc}")
                report["skipped"] += 1
                continue
            if params.name in existing and mode == "skip":
                # Existing hook (keyed by name) left untouched — not re-registered, not re-validated.
                report["skipped_existing"] += 1
                continue
            if params.topic in unlocked_topics:
                # Lock absent: writing the hook would hand an unverified public door its execution key.
                report["errors"].append(
                    f"hook {name!r}: topic {params.topic!r} is not restored — its verifier binding failed, "
                    "which would leave this hook live on an unverified public door"
                )
                report["skipped"] += 1
                continue
            try:
                # A stored hook must name a usable, token-free-evaluable execution key,
                # asserted exactly as the register door does (pass-role half not asserted:
                # this route is admin-only fenced).
                await scan.assert_usable(params.execution_key, bound_fingerprint=params.execution_key_fingerprint)
            except (ExecutionKeyAuthorityError, TokenFreeConditionError) as exc:
                # Only these two are per-record faults; other types propagate as the section's failure.
                report["errors"].append(f"hook {name!r}: {exc}")
                report["skipped"] += 1
                continue
            try:
                await manager.register(params)
            except ValueError as exc:
                # A non-compiling inline condition/expr jq is a per-hook rejection; other
                # types (store/transport) propagate as the section's failure.
                report["errors"].append(f"hook {name!r}: {exc}")
                report["skipped"] += 1
                continue
            if params.name in existing:
                report["updated"] += 1
            else:
                report["created"] += 1

    # A bare LIST is the hooks-only backup shape (no trigger-link envelope); restore the hooks directly.
    if isinstance(payload, list):
        await _restore_hooks(payload)
        return report

    if not isinstance(payload, dict):
        raise ValueError(
            f"webhooks section payload must be a list (old shape) or an envelope dict, got {type(payload)}"
        )
    # A missing key or non-list value raises loudly BEFORE any write — never a
    # silently default-empty section, never a bad type failing deeper in.
    for key in ("hooks", "trigger_links", "tombstones"):
        if key not in payload:
            raise ValueError(f"webhooks envelope is missing the required {key!r} key")
        if not isinstance(payload[key], list):
            raise ValueError(f"webhooks envelope {key!r} must be a list")
    if "topic_verifiers" not in payload:
        raise ValueError("webhooks envelope is missing the required 'topic_verifiers' key")
    if not isinstance(payload["topic_verifiers"], dict):
        raise ValueError("webhooks envelope 'topic_verifiers' must be a mapping of topic to binding")
    hooks = payload["hooks"]
    topic_verifiers = payload["topic_verifiers"]
    trigger_links = payload["trigger_links"]
    tombstones = payload["tombstones"]

    # Whole-section pre-write scan, touching zero keys: refuse a payload binding ONE
    # hash under TWO names, internally or against the LIVE index (a later revoke treats
    # an orphan binding as authoritative and would destroy the new name's record). A
    # NAME appearing twice is fine — it resolves last-wins through displacement.
    live_link_names = await bound_hashes_by_name()
    _reject_duplicate_hash_binding(trigger_links, live_link_names)

    # Ingress locks go back BEFORE the records they gate: no window in which a restored
    # hook is reachable through a door the backup had verified. Topics whose lock failed
    # are carried forward and every record ON them is refused below.
    unlocked_topics: set[str] = set()
    existing_verifiers = await manager.all_topic_verifiers()
    for topic, binding in topic_verifiers.items():
        if not isinstance(topic, str) or not topic:
            report["errors"].append(f"topic verifier with missing or empty topic: {topic!r}")
            report["skipped"] += 1
            continue
        if topic in existing_verifiers and mode == "skip":
            # Existing verifier binding left in place; the lock is present, so records on
            # this topic are NOT treated as unlocked below.
            report["skipped_existing"] += 1
            continue
        try:
            # Validates the binding shape on write: a bad entry is a per-topic rejection.
            await manager.set_topic_verifier(topic, binding)
        except ValidationError as exc:
            report["errors"].append(f"topic verifier {topic!r}: {exc}")
            report["skipped"] += 1
            unlocked_topics.add(topic)
            continue
        if topic in existing_verifiers:
            report["updated"] += 1
        else:
            report["created"] += 1

    await _restore_hooks(hooks, frozenset(unlocked_topics))

    # Tombstones first: a tombstoned hash then refuses its own record below (tombstone
    # wins). An idempotent set-union keyed by ``token_hash`` — ``skip``/``overwrite`` are moot.
    for token_hash in tombstones:
        try:
            await restore_tombstone(token_hash)
        except TriggerLinkError as exc:
            report["errors"].append(f"tombstone {token_hash!r}: {exc.message}")
            report["skipped"] += 1

    for item in trigger_links:
        name = item.get("name") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise TriggerLinkError(400, "trigger link entry must be a JSON object")
            if item["name"] in live_link_names and mode == "skip":
                # Existing trigger link (keyed by name) left untouched — its live record
                # and token hash stand, so a re-import does not re-key it.
                report["skipped_existing"] += 1
                continue
            record = item["record"]
            topic = record.get("topic") if isinstance(record, dict) else None
            if topic in unlocked_topics:
                # Lock absent: restoring the link would put it back on a verified door.
                raise TriggerLinkError(
                    400,
                    f"topic {topic!r} is not restored — its verifier binding failed, which would take this "
                    "link back into service on an unverified public door",
                )
            outcome = await restore_trigger_link(
                name=item["name"], token_hash=item["token_hash"], record=record, scan=scan
            )
        except (TriggerLinkError, KeyError) as exc:
            message = exc.message if isinstance(exc, TriggerLinkError) else f"missing key {exc}"
            report["errors"].append(f"trigger link {name!r}: {message}")
            report["skipped"] += 1
            continue
        if outcome in ("skipped_expired", "skipped_tombstoned"):
            report["skipped"] += 1
        elif outcome == "updated":
            report["updated"] += 1
        else:
            report["created"] += 1
    return report


def _reject_duplicate_hash_binding(trigger_links: Any, live_by_name: dict[str, str]) -> None:
    """Raise if any single token hash is bound under two DIFFERENT names, within the
    payload or against the live store index — zero keys written. ``live_by_name`` maps
    every ``name:*`` store binding (orphans included) to its hash, so binding a new name
    to an already-orphaned hash is refused too (a later revoke would destroy the new
    name's live record)."""
    # Malformed entries are left to the per-item restore to reject loudly.
    hash_to_name: dict[str, str] = {}
    for item in trigger_links:
        if not isinstance(item, dict):
            continue
        name, token_hash = item.get("name"), item.get("token_hash")
        if not isinstance(name, str) or not isinstance(token_hash, str):
            continue
        prior = hash_to_name.get(token_hash)
        if prior is not None and prior != name:
            raise ValueError(
                f"webhooks envelope binds token hash {token_hash!r} under two names ({prior!r} and {name!r})"
            )
        hash_to_name[token_hash] = name

    live_by_hash = {token_hash: name for name, token_hash in live_by_name.items()}
    for token_hash, name in hash_to_name.items():
        live_name = live_by_hash.get(token_hash)
        if live_name is not None and live_name != name:
            raise ValueError(
                f"import binds token hash {token_hash!r} to {name!r} but it is already live under {live_name!r}"
            )


# -- conversations -----------------------------------------------------------


async def _export_conversations() -> dict[str, Any]:
    from tai42_skeleton.conversations.backup import export_conversation_routes

    return await export_conversation_routes()


async def _import_conversations(payload: dict[str, Any]) -> _SectionReport:
    from tai42_skeleton.conversations.backup import import_conversation_routes

    return await import_conversation_routes(payload, current_import_mode())


# -- templates ---------------------------------------------------------------


async def _export_templates() -> dict[str, str]:
    resource_manager = tai42_app.storage.resource_manager
    paths = await resource_manager.list_resources()
    return {path: await resource_manager.fetch_template(path) for path in paths}


async def _import_templates(payload: dict[str, str]) -> _SectionReport:
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
            report["errors"].append(f"template {path!r}: {exc}")
            report["skipped"] += 1
            logger.warning("backup restore skipped unsafe template path %r: %s", path, exc)
            continue
        if path in existing and mode == "skip":
            # Existing template (keyed by path) left untouched.
            report["skipped_existing"] += 1
            continue
        await resource_manager.upload_template(path, content)
        if path in existing:
            report["updated"] += 1
        else:
            report["created"] += 1
    return report


# -- schedules ---------------------------------------------------------------


async def _export_schedules() -> Any:
    # Opaque: the backend owns the document shape. An unbound backend has no export
    # tool, so run_tool raises the unknown-tool error and the router records it per-section.
    return await tai42_app.tools.run_tool("backend_export_schedules", {})


async def _import_schedules(payload: Any) -> _SectionReport:
    # The document is opaque, but the mode is forwarded explicitly across the tool
    # boundary (a backend cannot read this process's import-mode context). A backend
    # whose import tool predates ``mode`` rejects it — a loud per-section error, not a
    # silent mode mismatch.
    return await tai42_app.tools.run_tool(
        "backend_import_schedules", {"schedules": payload, "mode": current_import_mode()}
    )


# -- connector_categories / connector_connections ----------------------------


async def _export_connector_categories() -> dict[str, Any]:
    from tai42_skeleton.connectors.store.backup import export_connector_categories

    return await export_connector_categories()


async def _import_connector_categories(payload: dict[str, Any]) -> _SectionReport:
    from tai42_skeleton.connectors.store.backup import import_connector_categories

    return await import_connector_categories(payload, current_import_mode())


async def _export_connector_connections() -> list[dict[str, Any]]:
    from tai42_skeleton.connectors.store.backup import export_connector_connections

    return await export_connector_connections()


async def _import_connector_connections(payload: list[dict[str, Any]]) -> _SectionReport:
    from tai42_skeleton.connectors.store.backup import import_connector_connections

    return await import_connector_connections(payload, current_import_mode())


# -- versioned_documents (the kind-agnostic versioned-document store) ---------


async def _export_versioned_documents() -> dict[str, Any]:
    from tai42_skeleton.versioning.backup import export_versioned_documents

    return await export_versioned_documents()


async def _import_versioned_documents(payload: dict[str, Any]) -> _SectionReport:
    from tai42_skeleton.versioning.backup import import_versioned_documents

    return await import_versioned_documents(payload, current_import_mode())


# -- tool_meta (the folder tree + per-tool organizational overlay) ------------


async def _export_tool_meta() -> dict[str, Any]:
    from tai42_skeleton.tool_meta.backup import export_tool_meta

    return await export_tool_meta()


async def _import_tool_meta(payload: dict[str, Any]) -> _SectionReport:
    from tai42_skeleton.tool_meta.backup import import_tool_meta

    return await import_tool_meta(payload, current_import_mode())


# -- registration ------------------------------------------------------------


def register_core_sections(registry: Any) -> None:
    """Register the skeleton's built-in sections on ``registry``.

    Called once per app-object construction, so an in-place reload (same app object)
    never re-registers. Registration order IS an import's replay order: a section
    deciding its records against live state another section restores is registered AFTER it.
    """
    registry.register_section("manifest", _export_manifest, _import_manifest)
    registry.register_section("env", _export_env, _import_env, secret=True)
    registry.register_section("access_control", _export_access_control, _import_access_control, secret=True)
    registry.register_section("sub_mcp", _export_sub_mcp, _import_sub_mcp)
    # Before ``webhooks``/``conversations``: their token-free scan renders policy
    # conditions carried by ``condition_id``, which are templates this section restores.
    registry.register_section("templates", _export_templates, _import_templates)
    # AFTER ``access_control`` and ``templates`` (records decided against the live policy
    # store, which those restore). secret=True: the bulk export aggregates hook
    # ``tool_kwargs`` and full token hashes, broader than the grantable per-record read.
    registry.register_section("webhooks", _export_webhooks, _import_webhooks, secret=True)
    # AFTER ``access_control``/``templates``, as ``webhooks``. secret=False: export equals the
    # grantable route-list read (``callback_secret`` excluded, re-minted only on overwrite).
    registry.register_section("conversations", _export_conversations, _import_conversations)
    # Unconditional: an unbound scheduling backend surfaces as a per-section error, not a gap.
    registry.register_section("schedules", _export_schedules, _import_schedules, secret=True)
    registry.register_section("connector_categories", _export_connector_categories, _import_connector_categories)
    registry.register_section(
        "connector_connections", _export_connector_connections, _import_connector_connections, secret=True
    )
    # Body-opaque and kind-agnostic, so ONE section covers every kind. secret=True:
    # at least one kind is secret-bearing (a preset's ``fixed_kwargs``, an AC-policy body).
    registry.register_section(
        "versioned_documents", _export_versioned_documents, _import_versioned_documents, secret=True
    )
    # Tool-metadata overlay (folders + per-tool rows). Not secret-bearing.
    registry.register_section("tool_meta", _export_tool_meta, _import_tool_meta)
