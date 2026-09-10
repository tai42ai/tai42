"""The ``states`` backup section — the platform state store's export/import.

Registers a section on the host's ``AppBackup`` registry (through the contract facet
``tai42_app.backup``) so ``tai backup export/import`` — and the Studio backup screen —
carry a deployment's declarations, templates, attachments, records and subject aliases
across deployments. The write-provenance ledger (``state_writes``) is deliberately
excluded — it is the audit of a running system, re-derived as writes land, never restored.

``secret=False``: the payload is operator configuration plus the subject documents the
store holds; it carries no credentials.

EXPORT reads the store directly; IMPORT writes through the facet doors
(``put_template`` / ``put_declaration`` / ``attach``) plus this section's own
``restore_aliases`` / ``restore_records`` paths so every validation applies unchanged and
each imported write is stamped with the
completed origin. A refused entity is REPORTED and skipped — one bad row never aborts the
rest. Order is load-bearing: templates → declarations → attachments → aliases → records (an
attachment needs its template and declaration; a record needs its declared state).

FEATURE GATE: with the ``states`` component's database unbound the exporter answers an
EMPTY payload and the importer refuses loudly (restoring into a feature that cannot serve
it would silently strand it).
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.states.errors import StatesError
from tai42_contract.states.models import AttachBody, StateDeclaration, StateTemplateDocument, WriteOrigin

from tai42_skeleton.states.db import states_store_configured
from tai42_skeleton.states.store import PostgresStatesStore

_SECTION = "states"
_VERSION = 1

# What a malformed or service-refused entity raises: the state store's own error family,
# plus the shape errors a hand-edited payload produces. Never a bare ``Exception``.
_ENTITY_ERRORS = (StatesError, KeyError, TypeError, ValueError)

_RESTORE_ORIGIN = WriteOrigin(consumer="backup-restore")


async def export_states() -> dict[str, Any]:
    """The section exporter: every template, declaration, attachment, record and alias — or
    an empty payload when the feature is off."""
    empty = {"version": _VERSION, "templates": [], "declarations": [], "attachments": [], "aliases": [], "records": []}
    if not states_store_configured():
        return empty
    store = PostgresStatesStore()
    templates = [row["body"] for row in await store.list_templates()]
    declarations: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for decl in await store.list_declarations():
        state = decl["name"]
        declarations.append(
            {
                "name": state,
                "description": decl.get("description") or "",
                "schema": decl["schema"],
                "subject_kinds": list(decl["subject_kinds"]),
                "default_subject_kind": decl["default_subject_kind"],
                "retention_days": decl.get("retention_days"),
            }
        )
        for attachment in await store.list_attachments_for_state(state):
            attachments.append(
                {
                    "state": state,
                    "template": attachment["template"],
                    "path": list(attachment["path"]),
                    "parameters": dict(attachment["parameters"] or {}),
                    "declarations": dict(attachment["declarations"] or {}),
                }
            )
        for alias in await store.list_aliases(state):
            aliases.append({"state": state, **alias})
        for record in await store.export_records(state):
            records.append({"state": state, **record})
    return {
        "version": _VERSION,
        "templates": templates,
        "declarations": declarations,
        "attachments": attachments,
        "aliases": aliases,
        "records": records,
    }


async def import_states(payload: dict[str, Any]) -> dict[str, Any]:
    """The section importer: upsert templates, declarations, attachments, aliases, then
    records through the facet doors, reporting per-entity outcomes. A newer payload version
    is refused."""
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"states backup payload carries no valid version (got {version!r})")
    if version > _VERSION:
        raise ValueError(f"states backup payload version {version!r} is newer than this build supports ({_VERSION})")
    if not states_store_configured():
        raise RuntimeError(
            "cannot import the states section: bind the 'states' component's database to enable the feature"
        )
    report: dict[str, Any] = {
        "templates": {"created": 0, "updated": 0, "failed": 0},
        "declarations": {"created": 0, "updated": 0, "failed": 0},
        "attachments": {"created": 0, "updated": 0, "failed": 0},
        "aliases": {"restored": 0, "failed": 0},
        "records": {"restored": 0, "failed": 0},
        "errors": [],
    }
    for entry in payload.get("templates") or []:
        try:
            existed = await tai42_app.states.get_template(entry.get("name")) is not None
            await tai42_app.states.put_template(StateTemplateDocument.model_validate(entry), replace=True)
        except _ENTITY_ERRORS as exc:
            report["templates"]["failed"] += 1
            report["errors"].append(f"template {entry.get('name')!r}: {exc}")
            continue
        report["templates"]["updated" if existed else "created"] += 1
    for entry in payload.get("declarations") or []:
        try:
            existed = await tai42_app.states.get_declaration(entry["name"]) is not None
            await tai42_app.states.put_declaration(StateDeclaration.model_validate(entry))
        except _ENTITY_ERRORS as exc:
            report["declarations"]["failed"] += 1
            report["errors"].append(f"declaration {entry.get('name')!r}: {exc}")
            continue
        report["declarations"]["updated" if existed else "created"] += 1
    # A restored attachment is a snapshot, not a re-attach: it re-runs the validators (schema
    # integrity) but NOT the reconcilers (there is no live re-attach to reconcile against,
    # and attachments restore before records) — reached through the concrete skeleton facet.
    from tai42_skeleton.app import instance

    for entry in payload.get("attachments") or []:
        try:
            attachments = await tai42_app.states.list_attachments(entry["state"], template=entry["template"])
            if attachments:
                await instance.app.states.update_attachment_declarations(
                    entry["state"], entry["template"], entry.get("declarations") or {}, skip_reconcilers=True
                )
            else:
                await instance.app.states.attach(
                    entry["state"],
                    entry["template"],
                    AttachBody(
                        path=entry.get("path") or [],
                        parameters=entry.get("parameters") or {},
                        declarations=entry.get("declarations") or {},
                    ),
                    skip_reconcilers=True,
                )
        except _ENTITY_ERRORS as exc:
            report["attachments"]["failed"] += 1
            report["errors"].append(f"attachment {entry.get('state')!r} ← {entry.get('template')!r}: {exc}")
            continue
        report["attachments"]["updated" if attachments else "created"] += 1
    aliases_by_state: dict[str, list[dict[str, Any]]] = {}
    for entry in payload.get("aliases") or []:
        aliases_by_state.setdefault(entry["state"], []).append({k: v for k, v in entry.items() if k != "state"})
    for state, rows in aliases_by_state.items():
        try:
            # ``restore_aliases`` is the states section's own restore path (off the
            # ``AppStates`` protocol), reached through the concrete skeleton facet.
            from tai42_skeleton.app import instance

            await instance.app.states.restore_aliases(state, rows, origin=_RESTORE_ORIGIN)
        except _ENTITY_ERRORS as exc:
            report["aliases"]["failed"] += len(rows)
            report["errors"].append(f"aliases for state {state!r}: {exc}")
            continue
        report["aliases"]["restored"] += len(rows)
    records_by_state: dict[str, list[dict[str, Any]]] = {}
    for entry in payload.get("records") or []:
        records_by_state.setdefault(entry["state"], []).append({k: v for k, v in entry.items() if k != "state"})
    for state, rows in records_by_state.items():
        try:
            # ``restore_records`` is the states section's own restore path (off the
            # ``AppStates`` protocol), reached through the concrete skeleton facet.
            from tai42_skeleton.app import instance

            await instance.app.states.restore_records(state, rows, origin=_RESTORE_ORIGIN)
        except _ENTITY_ERRORS as exc:
            report["records"]["failed"] += len(rows)
            report["errors"].append(f"records for state {state!r}: {exc}")
            continue
        report["records"]["restored"] += len(rows)
    return report


def register_states_backup_section(registry: Any) -> None:
    """Register the ``states`` section on ``registry`` — called once per app construction,
    beside the host's core sections (never on reload)."""
    registry.register_section(_SECTION, export_states, import_states, secret=False)


__all__ = ["export_states", "import_states", "register_states_backup_section"]
