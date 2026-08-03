"""Backup/restore operations — list the sections and import a backup document.

Two operations over the live ``tai42_app.backup`` registry (host sections plus any
a plugin registered), each running its subsystem's export/import through a thin pair.

* ``list_sections`` returns the registered sections as ``{name, secret}``.
* ``import_backup`` imports each SELECTED section and collects a per-section report.
  The selection is a SET, replayed in REGISTRATION (dependency) order, never the
  caller's list order. A ``version`` other than 1 is a loud 400; an unknown, absent, or
  failing section carries its error in the report and makes ``ok: false`` — none is a
  transport error, the request being well-formed. Destructive AND authority-changing
  (a restore can mint keys / replace policy), so off the default MCP surface (tier 2)
  and reload-gated.

The export door is a downloadable-attachment route, so it stays a handler in the
router and reaches these shared helpers from here.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel
from tai42_contract.app import tai42_app

from tai42_skeleton.backup.registry import BackupMode, import_mode
from tai42_skeleton.operations import BadRequestError, operation

logger = logging.getLogger(__name__)

_DOCUMENT_VERSION = 1


class BackupImport(BaseModel):
    """Import request — a backup ``document`` produced by the export route, the
    section names to import from it, and the per-record ``mode``."""

    document: dict[str, Any]
    sections: list[str]
    # Per keyed record: ``skip`` (default) leaves an existing record untouched,
    # ``overwrite`` upserts it. Mode-less sections (manifest, env, tokens) ignore it.
    mode: BackupMode = "skip"


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when a section's exporter/importer was async; a sync
    section returns its result directly."""
    if inspect.isawaitable(value):
        return await value
    return value


def _registered_section_names() -> set[str]:
    return {info.name for info in tai42_app.backup.sections()}


def _import_order(requested: list[str]) -> list[str]:
    """``requested`` replayed in REGISTRATION order, with names this host does not
    register appended so each still gets its report.

    The list is a SET, and registration order is a declared DEPENDENCY order (e.g.
    ``access_control`` and ``templates`` before ``webhooks``, whose importer decides records
    against both live stores). Replaying in the caller's order would make one document
    produce different stored state depending only on how the name list was typed."""
    registered = [info.name for info in tai42_app.backup.sections()]
    selected = list(dict.fromkeys(requested))
    known = set(registered)
    return [name for name in registered if name in selected] + [name for name in selected if name not in known]


@operation(summary="List backup sections", tags=["backup"])
async def list_sections() -> list:
    return [{"name": info.name, "secret": info.secret} for info in tai42_app.backup.sections()]


@operation(
    summary="Import a backup document",
    tags=["backup"],
    destructive=True,
    authority_changing=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=BackupImport,
)
async def import_backup(document: dict[str, Any], sections: list[str], mode: BackupMode = "skip") -> dict:
    # The envelope shape is validated at the HTTP edge; the document CONTENT (version,
    # its sections map) is the operation's own validation, raising ``BadRequestError``.
    if document.get("version") != _DOCUMENT_VERSION:
        raise BadRequestError(f"unsupported backup document version: {document.get('version')!r}")
    document_sections = document.get("sections")
    if not isinstance(document_sections, dict):
        raise BadRequestError("document must contain a 'sections' object")

    registered = _registered_section_names()
    reports: dict[str, Any] = {}
    ok = True
    # One mode for the whole import, read from the request-scoped context (the
    # ``import_section`` seam carries no mode arg).
    with import_mode(mode):
        for name in _import_order(sections):
            if name not in registered:
                # Unknown section in a well-formed request — a report error, not a transport failure.
                reports[name] = _absent_section_report(f"unknown section: {name!r}")
                ok = False
                continue
            if name not in document_sections:
                reports[name] = _absent_section_report(f"section {name!r} is not present in the backup document")
                ok = False
                continue
            try:
                report = await _maybe_await(tai42_app.backup.import_section(name, document_sections[name]))
            except Exception as exc:
                # A raising importer is reported with zero counts plus its message (no
                # cross-record transaction, so records committed before the raise still
                # stand). Logged too, so a genuine code bug is visible server-side.
                logger.warning("backup import of section %r failed: %s", name, exc, exc_info=True)
                reports[name] = _absent_section_report(str(exc))
                ok = False
                continue
            reports[name] = report
            if report.get("errors"):
                ok = False

    return {"ok": ok, "sections": reports}


def _absent_section_report(error: str) -> dict[str, Any]:
    """A zero-count section report carrying a single ``error`` — for a section that
    never ran (unknown, absent, or whose importer raised). Mirrors the importer report
    shape so the per-section reports are uniform."""
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": [error]}
