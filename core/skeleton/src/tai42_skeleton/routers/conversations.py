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
  newest activity first, paged by ``?page=``/``?pageSize=`` and optionally filtered by
  ``?status=`` (a delivery-status the summary must match) and ``?address=`` (a substring of
  the thread-id client-address suffix). A filter is a bounded post-scan, so the envelope
  carries ``truncated``.
- ``GET /api/conversations/{route_name}/messages/search?q=`` (AUTHED, admin) — every record
  on the route whose inbound text or answer contains ``q``, across all its threads, paged the
  same way. A bounded scan, so the envelope carries ``truncated``.
- ``GET /api/conversations/{route_name}/transcript?thread_id=`` (AUTHED) — one thread's
  transcript, paged the same way, ordered by ``?order=asc|desc`` and optionally filtered by
  ``?q=`` (a bounded scan over the record text, so the envelope carries ``truncated``). The
  thread id is a query value because it holds a percent-encoded principal that no path
  spelling round-trips.
- ``DELETE /api/conversations/{route_name}/thread?thread_id=`` (AUTHED) — forget one
  thread: its agent checkpoint, its answer records and its thread indexes. Forgetting is
  absolute — a valid id on its own route always succeeds, ``removed=0`` when nothing is
  stored, never a 404; a route-keyed id must carry the route's ``bridge:{route_name}:``
  prefix or it is a 400, and a person thread not on the named route is a 404. A turn in
  flight on the thread is a 409. The thread id is a query value, the same as the transcript
  door, because it holds a percent-encoded principal that no path spelling round-trips.
- ``DELETE /api/conversations/persons/{person_id}`` (AUTHED) — erase a linked person
  ENTIRELY: its aggregated ``bridge:@person:{id}`` thread (checkpoint, records, indexes, mode
  override), its person row and every address→person index mapping. Idempotent — an already
  erased person is not a 404; a turn in flight on the aggregated thread is a 409. The person
  id rides the path (a uuid4, so it round-trips a path segment cleanly).
- ``POST /api/conversations/{route_name}/thread/messages`` (AUTHED) — send an operator
  message BY HAND into a thread (no turn runs), delivered as the route identity. Body is
  ``{thread_id, text, address, media?, options?}``; returns ``{message_id, thread_id}``. The
  ``thread_id`` and ``address`` ride the body because the id holds a percent-encoded
  principal; ``media``/``options`` are OPTIONAL richer-send forms delivered with the text.
- ``GET /api/conversations/{route_name}/thread/mode?thread_id=`` (AUTHED) — read a thread's
  control mode and its source (``thread`` override or ``route`` default).
- ``PUT /api/conversations/{route_name}/thread/mode`` (AUTHED) — set a thread's mode override
  from a ``{thread_id, mode}`` body.

The write doors (route create, route delete, thread delete, person delete, operator send,
mode set) share ONE authority: the grantable ``write`` action. The same write grant that
creates a route deletes routes, forgets threads, erases persons, sends operator messages and
sets a thread's mode — no per-thread owner check.

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

The two thread READ doors parse their query at the edge here, and both declare the query
they take as an operation ``request_model``, so — being reads — the emitted spec publishes
their ``in: query`` parameters: the listing's ``page``/``pageSize`` window, and the
transcript's window plus its REQUIRED ``thread_id`` and its ``order``. The delete door reads
that same ``thread_id`` from the query at the edge; being a WRITE method, an operation
``request_model`` would be documented as a body it has none of, so it declares the query
through the operation ``query_model`` instead — the generic channel that publishes a model's
fields as ``in: query`` parameters for ANY method.

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
from tai42_skeleton.operations.conversations import delete_conversation_person as _delete_conversation_person_op
from tai42_skeleton.operations.conversations import delete_conversation_route as _delete_conversation_route_op
from tai42_skeleton.operations.conversations import delete_conversation_thread as _delete_conversation_thread_op
from tai42_skeleton.operations.conversations import get_conversation_config as _get_conversation_config_op
from tai42_skeleton.operations.conversations import get_conversation_message as _get_conversation_message_op
from tai42_skeleton.operations.conversations import get_conversation_route as _get_conversation_route_op
from tai42_skeleton.operations.conversations import get_conversation_thread as _get_conversation_thread_op
from tai42_skeleton.operations.conversations import get_conversation_thread_mode as _get_conversation_thread_mode_op
from tai42_skeleton.operations.conversations import list_conversation_configs as _list_conversation_configs_op
from tai42_skeleton.operations.conversations import list_conversation_routes as _list_conversation_routes_op
from tai42_skeleton.operations.conversations import list_conversation_threads as _list_conversation_threads_op
from tai42_skeleton.operations.conversations import list_failed_conversations as _list_failed_conversations_op
from tai42_skeleton.operations.conversations import search_conversation_messages as _search_conversation_messages_op
from tai42_skeleton.operations.conversations import (
    send_conversation_thread_message as _send_conversation_thread_message_op,
)
from tai42_skeleton.operations.conversations import set_conversation_config as _set_conversation_config_op
from tai42_skeleton.operations.conversations import set_conversation_thread_mode as _set_conversation_thread_mode_op
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


async def _extract_page_window(request: Request) -> dict:
    """The ``?page=`` / ``?pageSize=`` window as the thread read doors' flat arguments (a
    GET reads its parameters from the query string, never a body). A non-integer is a loud
    400 here; the operation range-checks the pair and caps the size."""
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("pageSize", "50")
    try:
        return {"page": int(page), "page_size": int(page_size)}
    except ValueError as exc:
        raise BadRequestError(f"page and pageSize must be integers: page={page!r} pageSize={page_size!r}") from exc


async def _extract_message_search_query(request: Request) -> dict:
    """The route message-search door's REQUIRED ``?q=`` on top of the shared window. A missing
    one is a loud 400 here; a blank one is the operation's own 400."""
    q = request.query_params.get("q")
    if q is None:
        raise BadRequestError("q is required: GET /api/conversations/{route_name}/messages/search?q=...")
    return {**await _extract_page_window(request), "q": q}


# The admin-tier failed-delivery listing and the route message search sit on literal paths so
# the ``{route_name}`` get/delete doors above never capture them. Both are registered BEFORE
# the read-one door — the message search on ``messages/search`` so ``search`` is never captured
# as a ``{message_id}`` — for the same reason they read a literal segment.
list_failed_conversations = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_failed_conversations_op),
    path="/api/conversations/messages/failed",
    method="GET",
    action="read",
)

search_conversation_messages = register_operation_route(
    tai42_app,
    operation_metadata_of(_search_conversation_messages_op),
    path="/api/conversations/{route_name}/messages/search",
    method="GET",
    context_extractor=_extract_message_search_query,
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
    """The thread listing's shared ``?page=`` / ``?pageSize=`` window plus its optional
    ``?status=`` / ``?address=`` filters (a GET reads its parameters from the query string).
    The two filters are passed through raw — the operation validates ``status`` against the
    delivery-status vocabulary (400 on unknown) and treats a blank filter as absent. The
    other read doors that only take the window parse it through :func:`_extract_page_window`
    directly, so they never inherit these filter kwargs."""
    return {
        **await _extract_page_window(request),
        "status": request.query_params.get("status"),
        "address": request.query_params.get("address"),
    }


async def _extract_transcript_query(request: Request) -> dict:
    """The transcript door's ``?thread_id=``, ``?order=`` and optional ``?q=`` on top of the
    shared window.

    The thread id rides the QUERY, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips — sent raw the
    server decodes it before routing, sent already-encoded the access-control path
    canonicalizer reads it as a doubly-encoded byte. A query value is decoded exactly once,
    by the query parser, whatever it holds. A missing one is a loud 400 here; a blank or
    unknown-order one is the operation's own 400."""
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: GET /api/conversations/{route_name}/transcript?thread_id=...")
    return {
        **await _extract_page_window(request),
        "thread_id": thread_id,
        "order": request.query_params.get("order", "asc"),
        "q": request.query_params.get("q"),
    }


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


async def _extract_thread_delete_query(request: Request) -> dict:
    """The delete door's ``?thread_id=``.

    The thread id rides the QUERY, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips — sent raw the
    server decodes it before routing, sent already-encoded the access-control path
    canonicalizer reads it as a doubly-encoded byte. A query value is decoded exactly once,
    by the query parser, whatever it holds. A missing one is a loud 400 here; a blank one is
    the operation's own 400."""
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: DELETE /api/conversations/{route_name}/thread?thread_id=...")
    return {"thread_id": thread_id}


# The item-level thread delete addresses the thread by ``?thread_id=`` query, the same as
# the transcript read door, because the id holds a percent-encoded principal that no path
# spelling round-trips. Its literal ``thread`` segment keeps it clear of the ``{route_name}``
# read/delete doors above.
delete_conversation_thread = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_thread_op),
    path="/api/conversations/{route_name}/thread",
    method="DELETE",
    context_extractor=_extract_thread_delete_query,
    action="write",
)

# Erasing a linked PERSON — its aggregated thread, its person row and every address→person
# index mapping — sits on its own ``persons/{person_id}`` literal first segment (``persons``
# can never be a route slug), clear of the ``{route_name}`` doors. It carries the SAME write
# action as the thread delete: the person id rides the path (a uuid4, no percent-encoded
# principal, so unlike a thread id it round-trips a path segment cleanly).
delete_conversation_person = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_person_op),
    path="/api/conversations/persons/{person_id}",
    method="DELETE",
    action="write",
)


async def _extract_thread_message(request: Request) -> dict:
    """The operator-send body ``{thread_id, text, address, media?, template?, options?}`` as
    the operation's flat fields. A non-object body, a missing/blank ``thread_id``, a
    non-string ``text``/``address``, a non-list ``media``/``options`` or a non-object
    ``template`` is a loud 400 here; the operation owns the blank-text, thread-belongs and
    media/template/options CONTENT guards (item shape, caps, exclusivity).

    ``thread_id`` rides the body, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError(
            "body must be a JSON object of {thread_id, text, address, media?, template?, options?}"
        ) from None
    thread_id = body.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise BadRequestError("thread_id is required and must be a non-blank string") from None
    text = body.get("text")
    if not isinstance(text, str):
        raise BadRequestError("text is required and must be a string") from None
    address = body.get("address")
    if address is not None and not isinstance(address, str):
        raise BadRequestError("address must be a string or absent") from None
    # Shape-only checks here; the operation coerces each media item through ``MediaItem`` and
    # applies the contract caps/exclusivity, mapping a violation to a 400.
    media = body.get("media")
    if media is not None and not isinstance(media, list):
        raise BadRequestError("media must be a list of media items or absent") from None
    template = body.get("template")
    if template is not None and not isinstance(template, dict):
        raise BadRequestError("template must be a template object or absent") from None
    options = body.get("options")
    if options is not None and not isinstance(options, list):
        raise BadRequestError("options must be a list of strings or absent") from None
    return {
        "thread_id": thread_id,
        "text": text,
        "address": address,
        "media": media,
        "template": template,
        "options": options,
    }


async def _extract_thread_mode_query(request: Request) -> dict:
    """The mode read door's ``?thread_id=``. Rides the query for the same reason the
    transcript door's does — the id carries a percent-encoded principal. A missing one is a
    loud 400 here; a blank one is the operation's own 400."""
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: GET /api/conversations/{route_name}/thread/mode?thread_id=...")
    return {"thread_id": thread_id}


async def _extract_thread_mode_body(request: Request) -> dict:
    """The mode write body ``{thread_id, mode}`` as the operation's flat fields. A non-object
    body, a missing/blank ``thread_id`` or a non-string ``mode`` is a loud 400 here; the
    operation validates the mode vocabulary and the thread-belongs guard."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object of {thread_id, mode}") from None
    thread_id = body.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise BadRequestError("thread_id is required and must be a non-blank string") from None
    mode = body.get("mode")
    if not isinstance(mode, str):
        raise BadRequestError("mode is required and must be a string") from None
    return {"thread_id": thread_id, "mode": mode}


# The operator-send and mode doors sit under the same literal ``thread`` segment as the
# thread delete, one level deeper (``/thread/messages``, ``/thread/mode``), so they never
# capture the ``{route_name}`` read/delete doors above.
send_conversation_thread_message = register_operation_route(
    tai42_app,
    operation_metadata_of(_send_conversation_thread_message_op),
    path="/api/conversations/{route_name}/thread/messages",
    method="POST",
    context_extractor=_extract_thread_message,
    action="write",
)

get_conversation_thread_mode = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_thread_mode_op),
    path="/api/conversations/{route_name}/thread/mode",
    method="GET",
    context_extractor=_extract_thread_mode_query,
    action="read",
)

set_conversation_thread_mode = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_conversation_thread_mode_op),
    path="/api/conversations/{route_name}/thread/mode",
    method="PUT",
    context_extractor=_extract_thread_mode_body,
    action="write",
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
        # VALUE-FREE: a params failure's pydantic error dict embeds ``input_value``, so the
        # body renders each error's ``msg`` only (which names the violated bound, never the
        # value) — never ``str(exc)`` or the error dicts, both of which reflect the input.
        detail = "; ".join(error["msg"] for error in exc.errors())
        return _error(f"invalid conversation message: {detail}", 400)

    cap = ConversationsSettings().sync_wait_max_seconds
    wait_seconds = 0 if message.wait_seconds is None else min(message.wait_seconds, cap)

    from tai42_skeleton.conversations import submit_api_message

    try:
        result = await submit_api_message(
            route_name,
            message.external_user_id,
            message.text,
            get_current_user_id(),
            wait_seconds,
            params=message.params,
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
async def _register_conversation_completion_tool() -> None:
    """Force-register the two hidden completion-delivery tools — the continuations a parked
    turn's resumed outcome is delivered through: ``conversation_deliver`` for a parked AGENT
    turn (the agent's own final answer) and ``deliver_tool_completion`` for a parked TOOL turn
    (the terminal outcome mapped through the route's ``reply_expr``).

    Both are mandatory bridge mechanisms (never operator-excludable catalog tools), so each is
    registered ``force=True`` and ``tai42/hidden`` — never offered to a model, reached only
    when a resumed run's resumer fires it. Registered whenever the bridge is wired, so the
    completion continuation the turn engine binds always resolves."""
    from tai42_skeleton.conversations.turn import (
        COMPLETION_TOOL_NAME,
        DELIVER_TOOL_COMPLETION_NAME,
        deliver_agent_completion,
        deliver_tool_completion,
    )

    tai42_app.tools.tool(
        deliver_agent_completion,
        name=COMPLETION_TOOL_NAME,
        tags={"conversations"},
        meta={"tai42/hidden": True},
        force=True,
    )
    tai42_app.tools.tool(
        deliver_tool_completion,
        name=DELIVER_TOOL_COMPLETION_NAME,
        tags={"conversations"},
        meta={"tai42/hidden": True},
        force=True,
    )


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
