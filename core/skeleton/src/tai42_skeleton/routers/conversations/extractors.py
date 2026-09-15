"""HTTP-edge body/query parsing for the conversation-route and config doors."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import ValidationError
from tai42_contract.conversations import ConversationRouteCreate, TargetConversationConfig

from tai42_skeleton.operations import BadRequestError

# The operator-send optional rich fields, each ``(field, expected type, 400 message)``.
# ``schema``/``template``/``location``/``header`` are objects, ``media``/``options``/
# ``sections`` are lists, ``footer`` a string; the operation owns the CONTENT guards.
_RICH_FIELD_SPECS: tuple[tuple[str, type, str], ...] = (
    ("media", list, "media must be a list of media items or absent"),
    ("template", dict, "template must be a template object or absent"),
    ("options", list, "options must be a list of strings or absent"),
    ("schema", dict, "schema must be a form answer-schema object or absent"),
    ("location", dict, "location must be a map-pin object or absent"),
    ("sections", list, "sections must be a list of option sections or absent"),
    ("header", dict, "header must be a media-header object or absent"),
    ("footer", str, "footer must be a string or absent"),
)


def _optional_typed(body: dict, field: str, expected: type, message: str) -> Any:
    """Return ``body[field]`` when present and of ``expected`` type, ``None`` when absent.

    Raises ``BadRequestError(message)`` when it is present but the wrong type.
    """
    value = body.get(field)
    if value is not None and not isinstance(value, expected):
        raise BadRequestError(message)
    return value


async def _extract_route_create(request: Request) -> dict:
    """Parse + validate the client-facing route body into the operation's flat fields.

    Rejects a malformed body with an explicit 400 (the adapter's plain parse would yield
    422).

    The ``route_name`` rides the URL path, not the body, so it is injected from the path
    param before validation — a body that also carries a ``route_name`` disagreeing with
    the path is rejected rather than silently overriding either.
    """
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


async def _extract_page_window(request: Request) -> dict:
    """The ``?page=`` / ``?pageSize=`` window as the thread read doors' flat arguments.

    A GET reads its parameters from the query string, never a body. A non-integer is a
    loud 400 here; the operation range-checks the pair and caps the size.
    """
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("pageSize", "50")
    try:
        return {"page": int(page), "page_size": int(page_size)}
    except ValueError as exc:
        raise BadRequestError(f"page and pageSize must be integers: page={page!r} pageSize={page_size!r}") from exc


async def _extract_message_search_query(request: Request) -> dict:
    """The route message-search door's REQUIRED ``?q=`` on top of the shared window.

    A missing one is a loud 400 here; a blank one is the operation's own 400.
    """
    q = request.query_params.get("q")
    if q is None:
        raise BadRequestError("q is required: GET /api/conversations/{route_name}/messages/search?q=...")
    return {**await _extract_page_window(request), "q": q}


async def _extract_paging(request: Request) -> dict:
    """The thread listing's shared ``?page=`` / ``?pageSize=`` window plus its optional filters.

    Adds the optional ``?status=`` / ``?address=`` filters (a GET reads its parameters
    from the query string). The two filters are passed through raw — the operation
    validates ``status`` against the delivery-status vocabulary (400 on unknown) and treats
    a blank filter as absent. The other read doors that only take the window parse it
    through :func:`_extract_page_window` directly, so they never inherit these filter
    kwargs.
    """
    return {
        **await _extract_page_window(request),
        "status": request.query_params.get("status"),
        "address": request.query_params.get("address"),
    }


async def _extract_transcript_query(request: Request) -> dict:
    """The transcript door's ``?thread_id=``, ``?order=`` and optional ``?q=`` on top of the shared window.

    The thread id rides the QUERY, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips — sent raw the
    server decodes it before routing, sent already-encoded the access-control path
    canonicalizer reads it as a doubly-encoded byte. A query value is decoded exactly once,
    by the query parser, whatever it holds. A missing one is a loud 400 here; a blank or
    unknown-order one is the operation's own 400.
    """
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: GET /api/conversations/{route_name}/transcript?thread_id=...")
    return {
        **await _extract_page_window(request),
        "thread_id": thread_id,
        "order": request.query_params.get("order", "asc"),
        "q": request.query_params.get("q"),
    }


async def _extract_thread_delete_query(request: Request) -> dict:
    """The delete door's ``?thread_id=``.

    The thread id rides the QUERY, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips — sent raw the
    server decodes it before routing, sent already-encoded the access-control path
    canonicalizer reads it as a doubly-encoded byte. A query value is decoded exactly once,
    by the query parser, whatever it holds. A missing one is a loud 400 here; a blank one is
    the operation's own 400.
    """
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: DELETE /api/conversations/{route_name}/thread?thread_id=...")
    return {"thread_id": thread_id}


async def _extract_person_locale_body(request: Request) -> dict:
    """The person-locale write body ``{locale}`` as the operation's ``locale`` field.

    A non-object body or a ``locale`` that is neither a string nor ``null`` is a loud 400
    here; the operation validates the tag itself. ``null`` clears the stored locale.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object of {locale}") from None
    locale = body.get("locale")
    if locale is not None and not isinstance(locale, str):
        raise BadRequestError("locale must be a BCP 47 string or null") from None
    return {"locale": locale}


async def _extract_thread_message(request: Request) -> dict:
    """The operator-send body as the operation's flat fields.

    The body is ``{thread_id, text, address, media?, template?, options?, schema?,
    location?, sections?, header?, footer?}``. A non-object body, a missing/blank
    ``thread_id``, a non-string ``text``/``address``/``footer``, a non-list
    ``media``/``options``/``sections`` or a non-object
    ``template``/``schema``/``location``/``header`` is a loud 400 here; the operation owns
    the blank-text, thread-belongs and rich-field CONTENT guards (item shape, caps,
    exclusivity, the composition matrix).

    ``thread_id`` rides the body, not the path: it carries the api door's percent-encoded
    ``{principal}/{end user}`` address, which no path spelling round-trips.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError(
            "body must be a JSON object of {thread_id, text, address, media?, template?, options?, "
            "schema?, location?, sections?, header?, footer?}"
        ) from None
    thread_id = body.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise BadRequestError("thread_id is required and must be a non-blank string") from None
    text = body.get("text")
    if not isinstance(text, str):
        raise BadRequestError("text is required and must be a string") from None
    # Shape-only checks here; the operation coerces each rich field through its typed model and
    # applies the contract caps/exclusivity/composition matrix, mapping a violation to a 400.
    result: dict[str, Any] = {
        "thread_id": thread_id,
        "text": text,
        "address": _optional_typed(body, "address", str, "address must be a string or absent"),
    }
    for field, expected, message in _RICH_FIELD_SPECS:
        result[field] = _optional_typed(body, field, expected, message)
    return result


async def _extract_thread_mode_query(request: Request) -> dict:
    """The mode read door's ``?thread_id=``.

    Rides the query for the same reason the transcript door's does — the id carries a
    percent-encoded principal. A missing one is a loud 400 here; a blank one is the
    operation's own 400.
    """
    thread_id = request.query_params.get("thread_id")
    if thread_id is None:
        raise BadRequestError("thread_id is required: GET /api/conversations/{route_name}/thread/mode?thread_id=...")
    return {"thread_id": thread_id}


async def _extract_thread_mode_body(request: Request) -> dict:
    """The mode write body ``{thread_id, mode}`` as the operation's flat fields.

    A non-object body, a missing/blank ``thread_id`` or a non-string ``mode`` is a loud
    400 here; the operation validates the mode vocabulary and the thread-belongs guard.
    """
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


async def _extract_target_config(request: Request) -> dict:
    """Parse + validate the ``TargetConversationConfig`` body into the operation's flat fields.

    Rejects a malformed body with an explicit 400 (the adapter's plain parse would yield
    422).

    ``target_kind`` and ``target_name`` ride the URL path, not the body, so they are
    injected from the path params before validation — a body that also carries either,
    disagreeing with the path, is rejected rather than silently overriding either.
    """
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
    flat = config.model_dump()
    # ``model_dump`` lowers the binding to a plain dict, but the operation's
    # ``state_binding`` parameter (and its attach-on-use) needs the parsed model — pass the
    # instance so a body binding is not dropped/mishandled at the route edge.
    flat["state_binding"] = config.state_binding
    return flat
