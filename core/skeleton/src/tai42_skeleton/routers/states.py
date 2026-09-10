"""HTTP routes for the subject-keyed state store — ``/api/states*``, the sibling
``/api/state-templates*`` and ``/api/state-retention/prune`` (all AUTHED).

Thin adapters over the operations in :mod:`tai42_skeleton.operations.states`; each door's
body/query is parsed at the HTTP edge into the operation's flat kwargs. The sibling
``/api/state-templates`` collection keeps the templates OFF the ``/api/states/{name}``
position, so a state literally named ``templates`` stays reachable at ``GET
/api/states/templates`` while ``GET /api/state-templates`` lists templates; the
literal-prefix sibling + retention routes register BEFORE the ``/api/states`` template
routes so the most-specific match is unambiguous.

Success bodies are ``{"data": ...}``; failures are ``{"error": "<message>"}`` (the states
OFF refusal also carries a stable ``code``).
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.states import (
    apply_state_record as _apply_state_record_op,
)
from tai42_skeleton.operations.states import (
    apply_state_template_jq as _apply_state_template_jq_op,
)
from tai42_skeleton.operations.states import (
    attach_state_template as _attach_state_template_op,
)
from tai42_skeleton.operations.states import (
    delete_state as _delete_state_op,
)
from tai42_skeleton.operations.states import (
    delete_state_template as _delete_state_template_op,
)
from tai42_skeleton.operations.states import (
    detach_state_template as _detach_state_template_op,
)
from tai42_skeleton.operations.states import (
    erase_state_record as _erase_state_record_op,
)
from tai42_skeleton.operations.states import (
    eval_state_template_jq as _eval_state_template_jq_op,
)
from tai42_skeleton.operations.states import (
    fold_state_record as _fold_state_record_op,
)
from tai42_skeleton.operations.states import (
    get_state as _get_state_op,
)
from tai42_skeleton.operations.states import (
    get_state_attachment as _get_state_attachment_op,
)
from tai42_skeleton.operations.states import (
    get_state_template as _get_state_template_op,
)
from tai42_skeleton.operations.states import (
    list_state_attachments as _list_state_attachments_op,
)
from tai42_skeleton.operations.states import (
    list_state_subjects as _list_state_subjects_op,
)
from tai42_skeleton.operations.states import (
    list_state_templates as _list_state_templates_op,
)
from tai42_skeleton.operations.states import (
    list_state_writes as _list_state_writes_op,
)
from tai42_skeleton.operations.states import (
    list_states as _list_states_op,
)
from tai42_skeleton.operations.states import (
    merge_state_record as _merge_state_record_op,
)
from tai42_skeleton.operations.states import (
    prune_state_retention as _prune_state_retention_op,
)
from tai42_skeleton.operations.states import (
    put_state as _put_state_op,
)
from tai42_skeleton.operations.states import (
    put_state_template as _put_state_template_op,
)
from tai42_skeleton.operations.states import (
    read_state_record as _read_state_record_op,
)
from tai42_skeleton.operations.states import (
    replace_state_record as _replace_state_record_op,
)
from tai42_skeleton.operations.states import (
    search_state_records as _search_state_records_op,
)
from tai42_skeleton.operations.states import (
    state_consumers as _state_consumers_op,
)
from tai42_skeleton.operations.states import (
    state_stats as _state_stats_op,
)
from tai42_skeleton.operations.states import (
    update_state_attachment as _update_state_attachment_op,
)

# -- request-edge readers -----------------------------------------------------


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc


async def _json_object(request: Request) -> dict[str, Any]:
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object")
    return body


def _require_object(body: dict[str, Any], field: str) -> dict[str, Any]:
    value = body.get(field)
    if not isinstance(value, dict):
        raise BadRequestError(f"body must contain a JSON object {field!r}")
    return value


def _require_list(body: dict[str, Any], field: str) -> list[Any]:
    value = body.get(field)
    if not isinstance(value, list):
        raise BadRequestError(f"body must contain a JSON array {field!r}")
    return value


def _require_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise BadRequestError(f"body must contain a non-empty string {field!r}")
    return value


def _optional_int(request: Request, field: str) -> int | None:
    raw = request.query_params.get(field)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise BadRequestError(f"{field!r} must be an integer") from exc


def _query_bool(request: Request, field: str) -> bool:
    return request.query_params.get(field, "").lower() in {"1", "true", "yes"}


# -- HTTP-edge extractors -----------------------------------------------------


async def _extract_declaration(request: Request) -> dict[str, Any]:
    return {"declaration": await _json_object(request)}


async def _extract_attach(request: Request) -> dict[str, Any]:
    return {"body": await _json_object(request)}


async def _extract_attach_declarations(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    ctx: dict[str, Any] = {"declarations": _require_object(body, "declarations")}
    if "options" in body:
        ctx["options"] = _require_object(body, "options")
    return ctx


async def _extract_subjects_query(request: Request) -> dict[str, Any]:
    return {
        "kind": request.query_params.get("kind"),
        "limit": _optional_int(request, "limit"),
        "cursor": request.query_params.get("cursor"),
    }


async def _extract_search(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    filters = body.get("filters", {})
    if not isinstance(filters, dict):
        raise BadRequestError("'filters' must be a JSON object")
    limit = body.get("limit")
    if limit is not None and not isinstance(limit, int):
        raise BadRequestError("'limit' must be an integer")
    cursor = body.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise BadRequestError("'cursor' must be a string")
    return {"filters": filters, "limit": limit, "cursor": cursor}


async def _extract_record_body(request: Request) -> dict[str, Any]:
    return {"data": await _json_object(request)}


async def _extract_record_patch(request: Request) -> dict[str, Any]:
    return {"patch": await _json_object(request)}


async def _extract_apply(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    op_id = body.get("op_id")
    if op_id is not None and not isinstance(op_id, str):
        raise BadRequestError("'op_id' must be a string")
    return {"ops": _require_list(body, "ops"), "op_id": op_id}


async def _extract_template_jq_params(request: Request) -> dict[str, Any]:
    """An input-purpose ``template_jq`` program's declared parameters from the query string:
    each ``?<name>=<json>`` is decoded as a JSON value (so a param may be any JSON, not only a
    string). A value that is not valid JSON is a loud 400."""
    params: dict[str, Any] = {}
    for name, raw in request.query_params.items():
        try:
            params[name] = json.loads(raw)
        except ValueError as exc:
            raise BadRequestError(f"template_jq param {name!r} must be a JSON value: {exc}") from exc
    return {"params": params}


async def _extract_template_jq_body(request: Request) -> dict[str, Any]:
    """An update-purpose ``template_jq`` apply body ``{input?, op_id?}`` — ``input`` (any JSON,
    the program's ``.input``, absent → null) and an optional string ``op_id`` idempotency
    key."""
    body = await _json_object(request)
    op_id = body.get("op_id")
    if op_id is not None and not isinstance(op_id, str):
        raise BadRequestError("'op_id' must be a string")
    return {"input": body.get("input"), "op_id": op_id}


async def _extract_fold(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    return {"into": _require_object(body, "into"), "mode": _require_str(body, "mode")}


async def _extract_writes_query(request: Request) -> dict[str, Any]:
    return {"limit": _optional_int(request, "limit"), "cursor": request.query_params.get("cursor")}


async def _extract_template_document(request: Request) -> dict[str, Any]:
    return {"document": await _json_object(request), "replace": _query_bool(request, "replace")}


# -- route registrations (literal siblings + retention FIRST) -----------------

# The single-record doors. ``{key}`` is one path segment addressed by its
# percent-encoded form: a subject key legitimately contains ``/`` (a thread key is
# ``bridge:{route}:{quote(principal)}/{external_user_id}``), so the caller encodes the
# slash as ``%2F``. ``use_raw_path_key`` below matches these doors on the raw path,
# keeping the key to one segment (and the ``…/writes`` sub-action unambiguous even for a
# key whose tail is ``writes``) before the key is decoded.
_RECORD_PATH = "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"


list_state_templates = register_operation_route(
    tai42_app, operation_metadata_of(_list_state_templates_op), path="/api/state-templates", method="GET", action="read"
)
get_state_template = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_state_template_op),
    path="/api/state-templates/{name}",
    method="GET",
    action="read",
)
put_state_template = register_operation_route(
    tai42_app,
    operation_metadata_of(_put_state_template_op),
    path="/api/state-templates/{name}",
    method="PUT",
    context_extractor=_extract_template_document,
    action="write",
)
delete_state_template = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_state_template_op),
    path="/api/state-templates/{name}",
    method="DELETE",
    action="write",
)
prune_state_retention = register_operation_route(
    tai42_app,
    operation_metadata_of(_prune_state_retention_op),
    path="/api/state-retention/prune",
    method="POST",
    action="write",
)

list_states = register_operation_route(
    tai42_app, operation_metadata_of(_list_states_op), path="/api/states", method="GET", action="read"
)
get_state = register_operation_route(
    tai42_app, operation_metadata_of(_get_state_op), path="/api/states/{name}", method="GET", action="read"
)
put_state = register_operation_route(
    tai42_app,
    operation_metadata_of(_put_state_op),
    path="/api/states/{name}",
    method="PUT",
    context_extractor=_extract_declaration,
    action="write",
)
delete_state = register_operation_route(
    tai42_app, operation_metadata_of(_delete_state_op), path="/api/states/{name}", method="DELETE", action="write"
)
state_stats = register_operation_route(
    tai42_app, operation_metadata_of(_state_stats_op), path="/api/states/{name}/stats", method="GET", action="read"
)
list_state_attachments = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_state_attachments_op),
    path="/api/states/{name}/attachments",
    method="GET",
    action="read",
)
get_state_attachment = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_state_attachment_op),
    path="/api/states/{name}/attachments/{template}",
    method="GET",
    action="read",
)
attach_state_template = register_operation_route(
    tai42_app,
    operation_metadata_of(_attach_state_template_op),
    path="/api/states/{name}/attachments/{template}",
    method="PUT",
    context_extractor=_extract_attach,
    action="write",
)
update_state_attachment = register_operation_route(
    tai42_app,
    operation_metadata_of(_update_state_attachment_op),
    path="/api/states/{name}/attachments/{template}",
    method="PATCH",
    context_extractor=_extract_attach_declarations,
    action="write",
)
detach_state_template = register_operation_route(
    tai42_app,
    operation_metadata_of(_detach_state_template_op),
    path="/api/states/{name}/attachments/{template}",
    method="DELETE",
    action="write",
)
list_state_subjects = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_state_subjects_op),
    path="/api/states/{name}/subjects",
    method="GET",
    context_extractor=_extract_subjects_query,
    action="read",
)
search_state_records = register_operation_route(
    tai42_app,
    operation_metadata_of(_search_state_records_op),
    path="/api/states/{name}/records/search",
    method="POST",
    context_extractor=_extract_search,
    # A POST search reads records but its authz class follows its method (the platform
    # ties POST to the write class, as the preset dry-run door does) — never a POST=read.
    action="write",
)
read_state_record = register_operation_route(
    tai42_app, operation_metadata_of(_read_state_record_op), path=_RECORD_PATH, method="GET", action="read"
)
replace_state_record = register_operation_route(
    tai42_app,
    operation_metadata_of(_replace_state_record_op),
    path=_RECORD_PATH,
    method="PUT",
    context_extractor=_extract_record_body,
    action="write",
)
merge_state_record = register_operation_route(
    tai42_app,
    operation_metadata_of(_merge_state_record_op),
    path=_RECORD_PATH,
    method="PATCH",
    context_extractor=_extract_record_patch,
    action="write",
)
apply_state_record = register_operation_route(
    tai42_app,
    operation_metadata_of(_apply_state_record_op),
    path=f"{_RECORD_PATH}/deltas",
    method="POST",
    context_extractor=_extract_apply,
    action="write",
)
erase_state_record = register_operation_route(
    tai42_app, operation_metadata_of(_erase_state_record_op), path=_RECORD_PATH, method="DELETE", action="write"
)
fold_state_record = register_operation_route(
    tai42_app,
    operation_metadata_of(_fold_state_record_op),
    path=f"{_RECORD_PATH}/fold",
    method="POST",
    context_extractor=_extract_fold,
    action="write",
)
list_state_writes = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_state_writes_op),
    path=f"{_RECORD_PATH}/writes",
    method="GET",
    context_extractor=_extract_writes_query,
    action="read",
)
# The template_jq sub-actions on a record. Registered BEFORE ``use_raw_path_key``
# below so the raw-path key covers them too (the ``{key}`` segment stays one segment even
# for a thread key carrying ``/``). The GET evaluates an input-purpose program (reads); the
# POST applies an update-purpose program, its authz following its method (the write class) —
# as the search POST does. Both share the ``…/template-jq/{program}`` path on their methods.
eval_state_template_jq = register_operation_route(
    tai42_app,
    operation_metadata_of(_eval_state_template_jq_op),
    path=f"{_RECORD_PATH}/template-jq/{{program}}",
    method="GET",
    context_extractor=_extract_template_jq_params,
    action="read",
)
apply_state_template_jq = register_operation_route(
    tai42_app,
    operation_metadata_of(_apply_state_template_jq_op),
    path=f"{_RECORD_PATH}/template-jq/{{program}}",
    method="POST",
    context_extractor=_extract_template_jq_body,
    action="write",
)

# Match every single-record door against the raw request path so a percent-encoded ``/``
# in the ``{key}`` segment (a thread key carries ``/``) keeps the key to ONE segment — see
# ``_RECORD_PATH`` above. Applied AFTER registration, over the shared record-path prefix, so
# it covers the plain doors and the ``/deltas`` /``/fold`` /``/writes`` sub-actions at once.
tai42_app.http.use_raw_path_key(_RECORD_PATH)
state_consumers = register_operation_route(
    tai42_app,
    operation_metadata_of(_state_consumers_op),
    path="/api/states/{name}/consumers",
    method="GET",
    action="read",
)
