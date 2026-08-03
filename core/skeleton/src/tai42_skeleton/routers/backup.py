"""HTTP surface for the backup/restore feature — the Studio's backup UI doors.

Three AUTHED doors over the live ``tai42_app.backup`` registry (host sections plus
any a plugin registered), each running its subsystem's export/import through a thin
pair. A secret-bearing export stays behind this credential like the config-env surface.

- ``GET  /api/backup/sections`` — the registered sections as ``{name, secret}``.
- ``POST /api/backup/export`` — body ``{"sections": [names]}``. An unregistered
  section is a loud 400. The response is a downloadable JSON document; a section whose
  exporter raises lands under ``errors`` and is omitted from ``sections`` — never a 500,
  never a silent drop.
- ``POST /api/backup/import`` — body ``{"document", "sections"}``. ``version`` other than
  1 is a loud 400. Each selected section is imported and its report collected; unknown,
  absent, or failing sections carry an error without being a transport failure. Response
  is 200 ``{"data": {"ok", "sections"}}``.

``list_sections`` and ``import_backup`` are thin adapters over
``tai42_skeleton.operations.backup`` (import's envelope shape validated here at the HTTP
edge). The export door stays a handler because its body is the raw document (a saved
``.json`` is exactly what ``import`` consumes), not a ``{"data": ...}`` envelope.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.backup import _DOCUMENT_VERSION, _maybe_await, _registered_section_names
from tai42_skeleton.operations.backup import import_backup as _import_backup_op
from tai42_skeleton.operations.backup import list_sections as _list_sections_op

logger = logging.getLogger(__name__)


class BackupExport(BaseModel):
    """Export request — the backup section names to include."""

    sections: list[str]


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _require_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


list_sections = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_sections_op),
    path="/api/backup/sections",
    method="GET",
    action="read",
)


@http_surface().custom_route(
    "/api/backup/export",
    methods=["POST"],
    summary="Export the named backup sections",
    tags=["backup"],
    request_model=BackupExport,
    response_model=None,
    action="fenced",
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=True,
        error_statuses=(400, 401, 500),
        success_status=200,
    ),
)
async def export_backup(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a JSON object", 400)
    requested = _require_string_list(body.get("sections"))
    if requested is None:
        return _error("body must contain a list of section-name strings 'sections'", 400)

    registered = _registered_section_names()
    unknown = [name for name in requested if name not in registered]
    if unknown:
        return _error(f"unknown section(s): {', '.join(sorted(unknown))}", 400)

    sections: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name in requested:
        try:
            payload = await _maybe_await(tai42_app.backup.export_section(name))
        except Exception as exc:  # absent subsystem — record, don't 500
            # Also logged, so a genuine exporter code bug is visible server-side.
            logger.warning("backup export of section %r failed: %s", name, exc, exc_info=True)
            errors[name] = str(exc)
            continue
        sections[name] = payload

    document = {
        "version": _DOCUMENT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "sections": sections,
        "errors": errors,
    }
    filename = f"tai-backup-{datetime.now(UTC).strftime('%Y-%m-%dT%H-%M-%SZ')}.json"
    return JSONResponse(document, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _extract_import(request: Request) -> dict:
    """Parse + validate the import body into the operation's flat
    ``document``/``sections``/``mode`` arguments, rejecting a malformed envelope with a
    loud 400 (the document CONTENT is the operation's own validation)."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object") from None
    document = body.get("document")
    if not isinstance(document, dict):
        raise BadRequestError("body must contain the export document object 'document'") from None
    selected = _require_string_list(body.get("sections"))
    if selected is None:
        raise BadRequestError("body must contain a list of section-name strings 'sections'") from None
    # ``mode`` defaults to ``skip``; an unrecognized value is a loud 400, not a silent fallback.
    mode = body.get("mode", "skip")
    if mode not in ("skip", "overwrite"):
        raise BadRequestError("'mode' must be 'skip' or 'overwrite'") from None
    return {"document": document, "sections": selected, "mode": mode}


import_backup = register_operation_route(
    tai42_app,
    operation_metadata_of(_import_backup_op),
    path="/api/backup/import",
    method="POST",
    context_extractor=_extract_import,
    action="fenced",
)
