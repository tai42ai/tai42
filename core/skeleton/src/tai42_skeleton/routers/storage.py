"""HTTP surface for the storage MANAGEMENT operations — ``/api/storage/{resources,entries,dirs}*``.

All routes are AUTHED.
AUTHED thin adapters over operations in ``tai42_skeleton.operations.storage`` — a thin
skin over the registered :class:`~tai42_contract.storage.Storage` provider (the app's
content store). Storage is dead by default (the skeleton ships no provider); a
backend registers one as a manifest-loaded plugin:

- ``GET /api/storage/resources`` — the sorted resource ids.
- ``GET /api/storage/resources/{id}/stat`` — the object's inferred content type.
- ``GET /api/storage/resources/{id}/content`` — the raw object bytes, as a file
  download. THIS door stays a native handler: it answers raw bytes + a
  Content-Disposition attachment header, not the ``{"data": ...}`` envelope.
- ``GET /api/storage/entries/{id}`` — the object as a text, document or binary entry.
- ``POST /api/storage/resources`` — upload text OR base64 bytes under an id.
- ``DELETE /api/storage/resources/{id}`` — remove one object.
- ``DELETE /api/storage/dirs/{path}`` — remove a directory subtree.

This router is optional (mounted under ``default_routers`` ``"all"``/``"api"`` or an
operator's ``routers_modules``). The presence read ``GET /api/storage`` is served by
the always-mounted core presence router (``routers.storage_presence``), so "is a
provider installed?" is answerable even where this management surface is absent.

With no provider registered every management door answers a loud 501. Success bodies
are ``{"data": ...}`` (or the raw download for content); failures are
``{"error": "<message>"}``. Every id or path input is a canonical store key, checked
by the shared key guard inside the operation (so the MCP tool edge and the CLI carry
it too); the download handler runs the same guard. The provider-error mapping lives in
the operations.
"""

from __future__ import annotations

import posixpath

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.adapter import read_json_object
from tai42_skeleton.operations.storage import _content_disposition
from tai42_skeleton.operations.storage import delete_dir as _delete_dir_op
from tai42_skeleton.operations.storage import delete_resource as _delete_resource_op
from tai42_skeleton.operations.storage import list_resources as _list_resources_op
from tai42_skeleton.operations.storage import read_resource as _read_resource_op
from tai42_skeleton.operations.storage import stat_resource as _stat_resource_op
from tai42_skeleton.operations.storage import upload_resource as _upload_resource_op
from tai42_skeleton.operations.storage_provider import NO_PROVIDER_MESSAGE, storage_provider
from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _extract_upload(request: Request) -> dict:
    """Parse the upload body at the HTTP edge, preserving the door's hand-authored malformed-body ``400``.

    The operation validates the field shapes.
    """
    body = await read_json_object(request)
    return {
        "resource_id": body.get("id"),
        "content_text": body.get("content_text"),
        "content_base64": body.get("content_base64"),
    }


list_resources = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_resources_op),
    path="/api/storage/resources",
    method="GET",
    action="read",
)

stat_resource = register_operation_route(
    tai42_app,
    operation_metadata_of(_stat_resource_op),
    path="/api/storage/resources/{resource_id:path}/stat",
    method="GET",
    action="read",
)

read_resource = register_operation_route(
    tai42_app,
    operation_metadata_of(_read_resource_op),
    path="/api/storage/entries/{resource_id:path}",
    method="GET",
    action="read",
)

upload_resource = register_operation_route(
    tai42_app,
    operation_metadata_of(_upload_resource_op),
    path="/api/storage/resources",
    method="POST",
    context_extractor=_extract_upload,
    action="write",
)

delete_resource = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_resource_op),
    path="/api/storage/resources/{resource_id:path}",
    method="DELETE",
    action="write",
)

delete_dir = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_dir_op),
    path="/api/storage/dirs/{dir_path:path}",
    method="DELETE",
    action="write",
)


@http_surface().custom_route(
    "/api/storage/resources/{resource_id:path}/content",
    methods=["GET"],
    summary="Download a storage resource",
    tags=["storage"],
    response_model=None,
    no_body_reason="Storage resource download: raw bytes",
    success_media_types={"GET": ("application/octet-stream",)},
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 401, 404, 500, 501),
        success_status=200,
    ),
    action="read",
)
async def download_resource(request: Request) -> Response:
    """Serve the raw object bytes as a file download.

    A content server (raw bytes + a Content-Disposition attachment header, not the
    ``{"data": ...}`` envelope), so it stays a native handler rather than an
    operation adapter.
    """
    resource_id = request.path_params["resource_id"]
    try:
        safe_template_path(resource_id)
    except UnsafeTemplatePathError as exc:
        return _error(str(exc), 400)
    provider = storage_provider()
    if provider is None:
        return _error(NO_PROVIDER_MESSAGE, 501)
    try:
        stored = await provider.load_object(resource_id)
    except FileNotFoundError:
        return _error(f"resource {resource_id!r} not found", 404)
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        return _error(str(exc), 400)
    filename = posixpath.basename(resource_id)
    return Response(
        content=stored.data,
        media_type=stored.content_type or "application/octet-stream",
        headers={"Content-Disposition": _content_disposition(filename)},
    )
