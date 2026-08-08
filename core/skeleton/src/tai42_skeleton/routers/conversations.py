"""HTTP surface for the conversation-route management feature — the authed CRUD doors
the operator and Studio drive over the routing table.

- ``GET /api/conversations`` (AUTHED) — list the stored routes, each with its
  ``callback_secret`` withheld.
- ``GET /api/conversations/{route_name}`` (AUTHED) — read one route by name; an unknown
  name is a loud 404.
- ``POST /api/conversations/{route_name}`` (AUTHED, ``authority_changing``) — create or
  replace a route from a ``ConversationRouteCreate`` body. An ``api`` row's minted
  ``callback_secret`` is returned ONCE here and never again. Binding the route's
  ``execution_key`` is a pass-role decision the operation takes before any write.
- ``DELETE /api/conversations/{route_name}`` (AUTHED) — delete a route by name; an
  unknown name is a loud 404.
- ``GET /api/conversations/{route_name}/threads`` (AUTHED, admin) — the route's threads,
  newest activity first, paged by ``?page=``/``?pageSize=``.
- ``GET /api/conversations/{route_name}/transcript?thread_id=`` (AUTHED) — one thread's
  transcript, caller-scoped, paged the same way and ordered by ``?order=asc|desc``. The
  thread id is a query value because it holds a percent-encoded principal that no path
  spelling round-trips.

- ``GET /api/conversation-configs`` (AUTHED) — list the per-target conversation configs
  (the ``multichannel`` opt-in + first-contact greeting), keyed ``(target_kind,
  target_name)``.
- ``GET /api/conversation-configs/{target_kind}/{target_name}`` (AUTHED) — read one; an
  unknown key is a loud 404.
- ``PUT /api/conversation-configs/{target_kind}/{target_name}`` (AUTHED) — create or replace
  one from a ``TargetConversationConfig`` body; the target must exist.
- ``DELETE /api/conversation-configs/{target_kind}/{target_name}`` (AUTHED) — delete one; an
  unknown key is a loud 404.

The config doors carry their own ``/api/conversation-configs`` prefix rather than nesting
under ``/api/conversations/{route_name}``, where a ``config`` first segment would collide
with the read-one route door.

Both thread doors parse their query at the edge here, and both declare the query they take
as an operation ``request_model``, so the emitted spec publishes their ``in: query``
parameters and a generated client sends the REQUIRED ``thread_id``.

Thin adapters over ``tai42_skeleton.operations.conversations`` — no routing logic here.
The POST body is structurally validated at the edge (a strict 400 surface); the operation
owns the logical guards. The channel door (``accept`` / ``record_delivery_status``) lives
on the ``conversations`` facet, not here.
"""

from __future__ import annotations

import logging

from fastapi import Request
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response
from tai42_contract.access_control import get_current_user_id
from tai42_contract.app import tai42_app
from tai42_contract.conversations import ConversationMessage, ConversationRouteCreate, TargetConversationConfig

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.conversations.caps import AddressRateLimitedError, ThreadQueueOverflowError
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import ConversationRouteResolutionError
from tai42_skeleton.operations import (
    BadRequestError,
    operation_metadata_of,
    register_operation_route,
)
from tai42_skeleton.operations.conversations import create_conversation_route as _create_conversation_route_op
from tai42_skeleton.operations.conversations import delete_conversation_config as _delete_conversation_config_op
from tai42_skeleton.operations.conversations import delete_conversation_route as _delete_conversation_route_op
from tai42_skeleton.operations.conversations import get_conversation_config as _get_conversation_config_op
from tai42_skeleton.operations.conversations import get_conversation_message as _get_conversation_message_op
from tai42_skeleton.operations.conversations import get_conversation_route as _get_conversation_route_op
from tai42_skeleton.operations.conversations import get_conversation_thread as _get_conversation_thread_op
from tai42_skeleton.operations.conversations import list_conversation_configs as _list_conversation_configs_op
from tai42_skeleton.operations.conversations import list_conversation_routes as _list_conversation_routes_op
from tai42_skeleton.operations.conversations import list_conversation_threads as _list_conversation_threads_op
from tai42_skeleton.operations.conversations import list_failed_conversations as _list_failed_conversations_op
from tai42_skeleton.operations.conversations import set_conversation_config as _set_conversation_config_op
from tai42_skeleton.operations.errors import NotSupportedError

logger = logging.getLogger(__name__)


async def _extract_route_create(request: Request) -> dict:
    """Parse + validate the client-facing route body into the operation's flat fields,
    rejecting a malformed body with an explicit 400 (the adapter's plain parse would
    yield 422).

    The ``route_name`` rides the URL path, not the body, so it is injected from the path
    param before validation — a body that also carries a ``route_name`` disagreeing with
    the path is rejected rather than silently overriding either."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object of route params") from None
    path_route_name = request.path_params["route_name"]
    body_route_name = body.get("route_name")
    if body_route_name is not None and body_route_name != path_route_name:
        raise BadRequestError("route_name in the body must match the route_name in the path") from None
    body = {**body, "route_name": path_route_name}
    try:
        create = ConversationRouteCreate.model_validate(body)
    except ValidationError as exc:
        raise BadRequestError(f"invalid conversation route: {exc}") from exc
    return create.model_dump()


list_conversation_routes = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_conversation_routes_op),
    path="/api/conversations",
    method="GET",
    action="read",
)

get_conversation_route = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_route_op),
    path="/api/conversations/{route_name}",
    method="GET",
    action="read",
)

create_conversation_route = register_operation_route(
    tai42_app,
    operation_metadata_of(_create_conversation_route_op),
    path="/api/conversations/{route_name}",
    method="POST",
    context_extractor=_extract_route_create,
    action="write",
)

delete_conversation_route = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_route_op),
    path="/api/conversations/{route_name}",
    method="DELETE",
    action="write",
)

# The admin-tier failed-delivery listing sits on a literal path so the ``{route_name}``
# get/delete doors above never capture it. It is registered BEFORE the read-one door for
# the same reason it reads a literal segment.
list_failed_conversations = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_failed_conversations_op),
    path="/api/conversations/messages/failed",
    method="GET",
    action="read",
)

get_conversation_message = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_message_op),
    path="/api/conversations/{route_name}/messages/{message_id}",
    method="GET",
    action="read",
)


async def _extract_paging(request: Request) -> dict:
    """The ``?page=`` / ``?pageSize=`` window as the thread read doors' flat arguments (a
    GET reads its parameters from the query string, never a body). A non-integer is a loud
    400 here; the operation range-checks the pair and caps the size."""
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("pageSize", "50")
    try:
        return {"page": int(page), "page_size": int(page_size)}
    except ValueError as exc:
        raise BadRequestError(f"page and pageSize must be integers: page={page!r} pageSize={page_size!r}") from exc


async def _extract_transcript_query(request: Request) -> dict:
    """The transcript door's ``?thread_id=`` and ``?order=`` on top of the shared window.

    The thread id rides the QUERY, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips — sent raw the
    server decodes it before routing, sent already-encoded the access-control path
    canonicalizer reads it as a doubly-encoded byte. A query value is decoded exactly once,
    by the query parser, whatever it holds. A missing one is a loud 400 here; a blank or
    unknown-order one is the operation's own 400."""
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: GET /api/conversations/{route_name}/transcript?thread_id=...")
    return {**await _extract_paging(request), "thread_id": thread_id, "order": request.query_params.get("order", "asc")}


list_conversation_threads = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_conversation_threads_op),
    path="/api/conversations/{route_name}/threads",
    method="GET",
    context_extractor=_extract_paging,
    action="read",
)

get_conversation_thread = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_thread_op),
    path="/api/conversations/{route_name}/transcript",
    method="GET",
    context_extractor=_extract_transcript_query,
    action="read",
)


async def _extract_target_config(request: Request) -> dict:
    """Parse + validate the ``TargetConversationConfig`` body into the operation's flat
    fields, rejecting a malformed body with an explicit 400 (the adapter's plain parse would
    yield 422).

    ``target_kind`` and ``target_name`` ride the URL path, not the body, so they are
    injected from the path params before validation — a body that also carries either,
    disagreeing with the path, is rejected rather than silently overriding either."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object of config params") from None
    for field in ("target_kind", "target_name"):
        path_value = request.path_params[field]
        body_value = body.get(field)
        if body_value is not None and body_value != path_value:
            raise BadRequestError(f"{field} in the body must match the {field} in the path") from None
        body = {**body, field: path_value}
    try:
        config = TargetConversationConfig.model_validate(body)
    except ValidationError as exc:
        raise BadRequestError(f"invalid conversation config: {exc}") from exc
    return config.model_dump()


list_conversation_configs = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_conversation_configs_op),
    path="/api/conversation-configs",
    method="GET",
    action="read",
)

get_conversation_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_config_op),
    path="/api/conversation-configs/{target_kind}/{target_name}",
    method="GET",
    action="read",
)

set_conversation_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_conversation_config_op),
    path="/api/conversation-configs/{target_kind}/{target_name}",
    method="PUT",
    context_extractor=_extract_target_config,
    action="write",
)

delete_conversation_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_config_op),
    path="/api/conversation-configs/{target_kind}/{target_name}",
    method="DELETE",
    action="write",
)


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


@http_surface().custom_route(
    "/api/conversations/{route_name}/messages",
    methods=["POST"],
    summary="Send a message to a conversation route",
    tags=["conversations"],
    request_model=ConversationMessage,
    response_model=None,
    declared=DeclaredRouteMetadata(
        reload_gated=True,
        reads_body=True,
        # The declared 503 is the plain-envelope one this handler answers on a full
        # thread queue; the reload gate's own 503 comes from ``reload_gated``.
        error_statuses=(400, 401, 404, 429, 501, 503),
        success_status=202,
        additional_success_statuses=(200,),
    ),
    action="write",
)
async def send_conversation_message(request: Request) -> Response:
    """Accept one authed message for ``route_name`` and run its turn AS the route's
    execution key.

    The auth gate authorizes who may SEND; the turn's own authority is the route's
    execution key, not the caller. Default answer is ``202 {message_id, thread_id}`` with
    the answer delivered later by signed callback. With a ``wait_seconds`` body field
    (clamped here to ``sync_wait_max_seconds``) a turn finishing in time answers ``200``
    inline — an answered/error turn with the answer, a silent turn with the silent marker
    (status ``silent``, no answer text) — and its callback (which otherwise carries
    answered/error/silent) is suppressed the same way, so it never double-fires; a turn
    still running when the wait elapses falls back to ``202``.
    """
    if reload_gate.locked:
        return reload_gate.reject_response()
    route_name = request.path_params["route_name"]
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a JSON object", 400)
    try:
        message = ConversationMessage.model_validate(body)
    except ValidationError as exc:
        return _error(f"invalid conversation message: {exc}", 400)

    cap = ConversationsSettings().sync_wait_max_seconds
    wait_seconds = 0 if message.wait_seconds is None else min(message.wait_seconds, cap)

    from tai42_skeleton.conversations import submit_api_message

    try:
        result = await submit_api_message(
            route_name, message.external_user_id, message.text, get_current_user_id(), wait_seconds
        )
    except ConversationRouteResolutionError as exc:
        return _error(str(exc), 404)
    except AddressRateLimitedError as exc:
        return _error(str(exc), 429)
    except ThreadQueueOverflowError as exc:
        return _error(str(exc), 503)
    except NotSupportedError as exc:
        return _error(str(exc), 501)

    payload: dict[str, object] = {"message_id": result.message_id, "thread_id": result.thread_id}
    if result.answer is not None:
        # ``exclude_none`` drops the ``answer`` field for a silent outcome, so a silent turn
        # answers 200 with ``{message_id, thread_id, status: "silent"}`` and no answer key.
        payload["answer"] = result.answer.model_dump(mode="json", exclude_none=True)
        return JSONResponse({"data": payload}, status_code=200)
    return JSONResponse({"data": payload}, status_code=202)


@tai42_app.lifecycle.on_startup
async def _redrive_pending_conversations() -> None:
    """Resume every unfinished conversation record on boot, so nothing is stranded
    across a restart.

    Intake re-drive must run FIRST: it gives every stranded ``accepted`` record a terminal
    outcome, which is work the delivery re-drive then picks up. No-op with no backend. The
    periodic sweep is established separately (post-swap), so it survives a reload rather
    than being spawned on the throwaway build-thread loop this handler runs on at reload."""
    if ConversationsSettings().in_memory:
        return
    from tai42_skeleton.conversations import redrive_accepted, redrive_pending

    await redrive_accepted()
    await redrive_pending()


@tai42_app.lifecycle.on_post_swap
def _start_conversations_delivery_sweep() -> None:
    """(Re)establish the periodic stalled-delivery sweep on the serving loop — run at
    boot and after every epoch swap, both ON the serving loop, so the sweep task
    attaches to the loop its deliveries run on and retires with its generation. The sweep
    is what recovers a record whose worker died holding a still-live lease. No-op with no
    backend."""
    if ConversationsSettings().in_memory:
        return
    from tai42_skeleton.conversations import start_delivery_sweep

    start_delivery_sweep()


@tai42_app.lifecycle.on_shutdown
async def _stop_conversations_delivery_sweep() -> None:
    """Cancel and await the stalled-delivery sweep on the serving loop it lives on. A
    backend-less deployment never started one."""
    if ConversationsSettings().in_memory:
        return
    from tai42_skeleton.conversations import stop_delivery_sweep

    await stop_delivery_sweep()
