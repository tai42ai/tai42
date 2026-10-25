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

import base64
import binascii
import logging
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import FanoutSummary, RecycleRoll
from tai42_contract.backend import (
    SCHEDULE_EXPORT_TOOL,
    SCHEDULE_IMPORT_TOOL,
    ScheduleImportError,
    ScheduleImportReport,
    ScheduleRecord,
)
from tai42_contract.backup import BackupSectionReport
from tai42_contract.interactions import ParkableDoorMixin
from tai42_contract.states import StateBinding
from tai42_contract.states.errors import StatesError
from tai42_kit.utils.schedule_subject import SCHEDULE_CONTRACT_ARG, SCHEDULE_STATE_BINDING_ARG

from tai42_skeleton.backup.registry import _empty_report, current_import_mode
from tai42_skeleton.backup.webhooks_section import _export_webhooks, _import_webhooks
from tai42_skeleton.schedules import check_schedule_definition
from tai42_skeleton.states.backup import register_states_backup_section
from tai42_skeleton.storage.prefix_marks import covering_prefix
from tai42_skeleton.tools.state_binding import is_binding_refusal

logger = logging.getLogger(__name__)

_SCHEDULE_RECORDS: TypeAdapter[list[ScheduleRecord]] = TypeAdapter(list[ScheduleRecord])


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
    return BackupSectionReport(
        updated=1, fanout=FanoutSummary.model_validate(result.fanout), span_release_error=result.span_release_error
    )


# -- env ---------------------------------------------------------------------


def _export_env() -> dict[str, str]:
    try:
        return tai42_app.config.config_manager.read_env()
    except FileNotFoundError:
        # No env file yet is a normal empty state, not an error.
        return {}


async def _import_env(payload: dict[str, str]) -> BackupSectionReport:
    from tai42_skeleton.config.service import ConfigService
    from tai42_skeleton.operations._broadcast import result_recycle_roll

    config_manager = tai42_app.config.config_manager
    try:
        existing = config_manager.read_env()
    except FileNotFoundError:
        existing = {}
    created = sum(1 for key in payload if key not in existing)
    updated = sum(1 for key in payload if key in existing)
    # Apply through the pipeline: validated, reloaded, broadcast to the fleet.
    result = await ConfigService.from_app().apply_env_change(payload)
    roll = result_recycle_roll(result)
    return BackupSectionReport(
        created=created,
        updated=updated,
        fanout=FanoutSummary.model_validate(result.fanout),
        recycle=None if roll is None else RecycleRoll.model_validate(roll),
        awaiting_respawn=list(result.awaiting_respawn),
        span_release_error=result.span_release_error,
    )


# -- access_control ----------------------------------------------------------


async def _export_access_control() -> dict[str, Any]:
    from tai42_skeleton.access_control.backup import export_access_control

    return await export_access_control()


async def _import_access_control(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_skeleton.access_control.backup import import_access_control

    return await import_access_control(payload, current_import_mode())


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


# -- storage -----------------------------------------------------------------


class _StoredObjectEntry(BaseModel):
    """One stored object in the ``storage`` section: its bytes as UTF-8 text or as base64, and its content type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_text: str | None = None
    content_base64: str | None = None
    content_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _an_object(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise PydanticCustomError("entry_type", "entry must be an object carrying content_text or content_base64")
        return value

    @model_validator(mode="after")
    def _exactly_one_content(self) -> Self:
        if (self.content_text is None) == (self.content_base64 is None):
            raise PydanticCustomError(
                "content_one_of", "entry must carry exactly one of content_text or content_base64"
            )
        return self


def _entry_problem(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" if error["loc"] else error["msg"]
        for error in exc.errors()
    )


def _stored_object_entry(data: bytes, content_type: str | None) -> dict[str, Any]:
    # Strict UTF-8 is the store's own definition of text; the decode is lossless, so
    # ``content_text`` reproduces the bytes exactly.
    try:
        return {"content_text": data.decode("utf-8"), "content_type": content_type}
    except UnicodeDecodeError:
        return {"content_base64": base64.b64encode(data).decode("ascii"), "content_type": content_type}


async def _export_storage() -> dict[str, dict[str, Any]]:
    resource_manager = tai42_app.storage.resource_manager
    not_backed_up = tai42_app.storage.not_backed_up_prefixes()
    payload: dict[str, dict[str, Any]] = {}
    for path in await resource_manager.list_resources():
        if covering_prefix(path, not_backed_up) is not None:
            continue
        data, content_type = await resource_manager.load_stored(path)
        payload[path] = _stored_object_entry(data, content_type)
    return payload


def _reject_template(report: BackupSectionReport, path: str, reason: object) -> None:
    # A per-path rejection is recorded loudly and the rest of the section still restores.
    report.errors.append(f"template {path!r}: {reason}")
    report.skipped += 1
    logger.warning("backup restore skipped template path %r: %s", path, reason)


def _accepted_storage_row(
    report: BackupSectionReport, path: str, raw_entry: Any, not_backed_up: frozenset[str]
) -> tuple[bytes, str | None] | None:
    """The bytes and content type a backup row restores, or ``None`` after recording its refusal."""
    from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path

    try:
        # An untrusted backup can carry a traversal key writing outside the store
        # root; guard each key BEFORE the write.
        safe_template_path(path)
    except UnsafeTemplatePathError as exc:
        _reject_template(report, path, exc)
        return None
    prefix = covering_prefix(path, not_backed_up)
    if prefix is not None:
        _reject_template(report, path, f"path is under {prefix!r}, which is marked not backed up")
        return None
    try:
        entry = _StoredObjectEntry.model_validate(raw_entry)
    except ValidationError as exc:
        _reject_template(report, path, _entry_problem(exc))
        return None
    if entry.content_text is not None:
        return entry.content_text.encode("utf-8"), entry.content_type
    try:
        return base64.b64decode(entry.content_base64 or "", validate=True), entry.content_type
    except binascii.Error as exc:
        _reject_template(report, path, f"content_base64 is not valid base64 ({exc})")
        return None


async def _import_storage(payload: dict[str, Any]) -> BackupSectionReport:
    from tai42_contract.storage import StoragePathConflictError

    resource_manager = tai42_app.storage.resource_manager
    not_backed_up = tai42_app.storage.not_backed_up_prefixes()
    existing = set(await resource_manager.list_resources())
    mode = current_import_mode()
    report = _empty_report()
    for path, raw_entry in payload.items():
        accepted = _accepted_storage_row(report, path, raw_entry, not_backed_up)
        if accepted is None:
            continue
        data, content_type = accepted
        if path in existing and mode == "skip":
            # Existing object (keyed by path) left untouched.
            report.details["skipped_existing"] += 1
            continue
        try:
            await resource_manager.upload_bytes(path, data, content_type)
        except StoragePathConflictError as exc:
            # A key colliding with a directory that still holds objects.
            _reject_template(report, path, exc)
            continue
        except UnicodeDecodeError:
            # A text-only store's bytes write strict-decodes UTF-8 and cannot hold this object.
            _reject_template(report, path, "the storage provider holds text only and cannot store this object")
            continue
        if path in existing:
            report.updated += 1
        else:
            report.created += 1
    return report


# -- schedules ---------------------------------------------------------------


async def _export_schedules() -> list[dict[str, Any]]:
    """The backend's export validated as ``list[ScheduleRecord]``, dumped.

    An unbound backend has no export tool, so ``run_tool`` raises the unknown-tool error and the
    router records it per-section; an export of another shape raises loudly the same way.
    """
    raw = await tai42_app.tools.run_tool(SCHEDULE_EXPORT_TOOL, {})
    records = _SCHEDULE_RECORDS.validate_python(raw)
    return [record.model_dump(mode="json") for record in records]


async def _check_schedule_rows(payload: Any) -> tuple[Any, list[int], list[dict[str, Any]]]:
    """Run the schedule definition check over every row carrying a door contract or a door binding.

    Returns the rows to forward to the backend, each forwarded row's index in ``payload``, and
    one ``{"index", "name", "error"}`` record per refused row. A ``ScheduleRecord`` row carries
    its contract and its binding under the reserved keys of its ``kwargs``; a row of any other
    shape carries neither in a form the platform can read and is forwarded untouched for the
    backend to judge (it refuses a row that is not a ``ScheduleRecord`` as a per-row error). A
    refusal (a malformed contract or binding, a stored id the store lacks, a binding the states
    store refuses) keeps the row from the backend; any other error fails the section.
    """
    if not isinstance(payload, list):
        return payload, [], []
    forwarded: list[Any] = []
    positions: list[int] = []
    refused: list[dict[str, Any]] = []
    for index, row in enumerate(payload):
        kwargs = row.get("kwargs") if isinstance(row, dict) else None
        raw = kwargs.get(SCHEDULE_STATE_BINDING_ARG) if isinstance(kwargs, dict) else None
        raw_contract = kwargs.get(SCHEDULE_CONTRACT_ARG) if isinstance(kwargs, dict) else None
        if raw is not None or raw_contract is not None:
            try:
                await check_schedule_definition(
                    StateBinding.model_validate(raw) if raw is not None else None,
                    ParkableDoorMixin.model_validate(raw_contract) if raw_contract is not None else None,
                )
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
    # The mode is forwarded explicitly across the tool boundary (a backend cannot read this
    # process's import-mode context); the backend validates each row as a ``ScheduleRecord``.
    forwarded, positions, refused = await _check_schedule_rows(payload)
    raw = await tai42_app.tools.run_tool(SCHEDULE_IMPORT_TOOL, {"schedules": forwarded, "mode": current_import_mode()})
    report = _schedules_report(raw, positions)
    report.errors = [_schedule_error_text(entry) for entry in refused] + report.errors
    report.skipped += len(refused)
    return report


def _schedules_report(raw: Any, positions: list[int]) -> BackupSectionReport:
    """Map the scheduling backend's import result into the typed section report.

    The result must be a ``ScheduleImportReport``: its counts fill the typed fields, its per-row
    errors (each rendered as a string) fill ``errors``, and ``skipped_existing`` rides ``details``.
    Any other shape raises ``TypeError`` naming the violation, so the section errors loudly rather
    than mis-reporting a restore. ``positions`` maps the backend's row index (over the rows it was
    handed) back to the row's index in the restored document.
    """
    try:
        report = ScheduleImportReport.model_validate(raw)
    except ValidationError as exc:
        raise TypeError(
            f"the scheduling backend's {SCHEDULE_IMPORT_TOOL} returned a report that is not ScheduleImportReport: {exc}"
        ) from exc
    return BackupSectionReport(
        created=report.created,
        updated=report.updated,
        skipped=report.skipped,
        errors=[_schedule_error_text(_document_row(entry, positions)) for entry in report.errors],
        details={"skipped_existing": report.skipped_existing},
    )


def _document_row(entry: ScheduleImportError, positions: list[int]) -> dict[str, Any]:
    """A backend error record re-indexed from the backend's row to the document's row.

    An out-of-range ``index`` is kept as the backend reported it.
    """
    row = entry.model_dump()
    if 0 <= entry.index < len(positions):
        row["index"] = positions[entry.index]
    return row


def _schedule_error_text(entry: dict[str, Any]) -> str:
    """Render one ``{"index", "name", "error"}`` error record as a string for the typed report."""
    return f"schedule {entry['name']!r} (row {entry['index']}): {entry['error']}"


# -- connector_categories / connector_connections / connector_oauth_clients ----


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


async def _export_connector_oauth_clients() -> list[dict[str, Any]]:
    from tai42_skeleton.connectors.store.backup import export_connector_oauth_clients

    return await export_connector_oauth_clients()


async def _import_connector_oauth_clients(payload: list[dict[str, Any]]) -> BackupSectionReport:
    from tai42_skeleton.connectors.store.backup import import_connector_oauth_clients

    return await import_connector_oauth_clients(payload, current_import_mode())


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
    registry.register_section("manifest", _export_manifest, _import_manifest)
    registry.register_section("env", _export_env, _import_env, secret=True)
    # Before every section whose records name a stored resource by id (access control,
    # states, webhooks, conversations, schedules): they are checked against the objects
    # this section restores.
    registry.register_section("storage", _export_storage, _import_storage)
    registry.register_section("access_control", _export_access_control, _import_access_control, secret=True)
    registry.register_section("sub_mcp", _export_sub_mcp, _import_sub_mcp)
    # States before every section that restores a binding: a restored binding attaches templates
    # on states, so the declarations and stored templates it names restore first.
    register_states_backup_section(registry)
    # AFTER ``access_control`` and ``storage`` (records decided against the live policy
    # store, which those restore). secret=True: the bulk export aggregates hook
    # ``tool_kwargs`` and full token hashes, broader than the grantable per-record read.
    registry.register_section("webhooks", _export_webhooks, _import_webhooks, secret=True)
    # AFTER ``access_control``/``storage``, as ``webhooks``. secret=False: export equals the
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
    registry.register_section(
        "connector_oauth_clients", _export_connector_oauth_clients, _import_connector_oauth_clients, secret=True
    )
    # Body-opaque and kind-agnostic, so ONE section covers every kind. secret=True:
    # the ``settings_profile`` kind stores env values verbatim, secrets included
    # (``SettingsProfileBody.env``), so the section is secret-bearing.
    registry.register_section(
        "versioned_documents", _export_versioned_documents, _import_versioned_documents, secret=True
    )
    # Tool-metadata overlay (folders + per-tool rows). Not secret-bearing.
    registry.register_section("tool_meta", _export_tool_meta, _import_tool_meta)
