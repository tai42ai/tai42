"""HTTP routes for the ask_user interactions surface — ``/api/interactions/*``.

Doors:

* ``GET /api/interactions`` — the authenticated PAGED pending-question list (the
  ``list_interactions`` operation): the initial-load surface a client reads before
  applying the live stream. Audience-filtered before paging so the totals are honest.
* ``GET /api/interactions/stream`` — the authenticated TAIL-ONLY SSE feed: a live
  tail of add/answered/removed events from the cursor captured at connect. It
  carries no historical backlog and no end-of-backlog marker (the paged list door
  is the initial-load surface).
* ``GET /api/interactions/media/{media_id}`` — the UNAUTHENTICATED served-media
  door: the media id is the capability secret (a vendor fetches the url from its
  own servers, a browser ``<img>`` from the inbox origin). Serves the stored bytes
  with their mime; a malformed id is a 400, a miss/expired id a 404.
* ``POST /api/interactions/{interaction_id}/answer`` — the authenticated human
  answer door. The value is validated server-side against the stored question's
  ``answer_format`` before the blocked caller is woken; an invalid answer is
  rejected loudly and the caller stays blocked. An EXTERNAL question is answered
  through its callback URL, never here.
* ``POST /api/interactions/callback/{ticket}`` — the UNAUTHENTICATED data door
  for external-format answers (the server-to-server / confirm-form claim path).
  Sensitive data rides the JSON body here.
* ``GET /api/interactions/callback/{ticket}`` — the UNAUTHENTICATED redirect
  door. GET never mutates state (link scanners prefetch these URLs). It serves a
  page by the question's answer format: for confirm/external the byte-constant
  confirm page whose form POSTs back to the same URL; for a channel-delivered
  ``form`` a schema-rendered HTML form that POSTs ``{"answer": {...}}`` as JSON
  to the same URL; for text/select an informational awaiting-reply page (a bare
  confirm tap carries no value answer).

The callback ticket is a bearer capability minted by the ``ask_user`` helper;
it is never deleted, single-use is enforced by the answered-state guard in
``record_answer``, and a duplicate callback resolves idempotently to 200. The
callback door is NOT audience-gated — the ticket IS the authorization (an
external-answer flow deliberately addresses outsiders). That is sound because the
ticket is delivered ONLY over the configured channel: no stream or read frame ever
carries it (``_add_data`` emits no ``ticket`` field), so a restricted caller
filtered out of a question's stream can never obtain that question's ticket, and
the answer-door audience gate cannot be bypassed through this door.

Success bodies are ``{"data": {...}}``; failures are ``{"error": "<message>"}``.
The paged list door and the live tail share the same add-frame shape (``_add_data``);
a client seeds from the list, then applies the tail, de-duplicating by
``interaction_id``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from tai42_contract.app import tai42_app
from tai42_contract.conversations import validate_entry_params
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionResponse,
    InteractionState,
)
from tai42_contract.webhooks import WebhookVerificationError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.app.epoch import mark_current_request_drain_exempt
from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.interactions.continuation import continuation_due_timing, fire_continuation_after_claim
from tai42_skeleton.interactions.form_schema import channel_form_fields
from tai42_skeleton.interactions.media import read_media
from tai42_skeleton.interactions.settings import (
    INTERACTIONS_NOT_CONFIGURED_CODE,
    INTERACTIONS_NOT_CONFIGURED_MESSAGE,
    InteractionsSettings,
    interactions_settings,
    interactions_store_configured,
)
from tai42_skeleton.interactions.store import (
    ADD_EVENT,
    ANSWERED_EVENT,
    REMOVED_EVENT,
    InteractionStore,
    as_str,
)
from tai42_skeleton.operations import (
    BadRequestError,
    PayloadTooLargeError,
    operation_metadata_of,
    register_operation_route,
)

# The human answer door is an operation in ``tai42_skeleton.operations.interactions``;
# the still-handler callback door shares its answer-validation, reply-TTL, and
# serializer-guarded claim helpers, imported from that module.
from tai42_skeleton.operations.interactions import (
    _add_data,
    _AnswerInvalid,
    _claim_or_serialization_error,
    _reply_ttl,
    _schema_mismatch,
    _validate_answer,
)
from tai42_skeleton.operations.interactions import answer_interaction as _answer_interaction_op
from tai42_skeleton.operations.interactions import list_interactions as _list_interactions_op
from tai42_skeleton.operations.interactions import list_pending_interactions as _list_pending_interactions_op

logger = logging.getLogger(__name__)

_KEEPALIVE_SECONDS = 15

# The first SSE frame every stream flushes at connect, BEFORE the tail's first
# (blocking) XREAD. An SSE comment (leading ``:``) is a no-op to EventSource and to a
# fetch-reader SSE client, but it makes the FIRST body byte arrive at connect time — so a
# client whose Fetch resolves the response only on the first body byte (Firefox; Chromium
# resolves on the headers) treats the stream as connected in ~0.1s instead of waiting up to
# ``_KEEPALIVE_SECONDS`` for the first keepalive. Without it, `useInteractionsStream`'s
# connect-time base refetch — which fires when the stream fetch resolves — is deferred a
# full keepalive interval on Firefox, leaving the inbox's pending list up to 15s stale on
# every connect.
_CONNECT_FRAME = ": connected\n\n"


def _now() -> float:
    """The monotonic loop clock the keepalive deadline reads. A module-level seam so
    a test can drive the deadline without real wall-clock waits."""
    return asyncio.get_running_loop().time()


# no-store + nosniff ride EVERY callback response: capability-bearing URLs must
# never be cached, and the schema-mismatch 400 reflects attacker-influenced
# content that browsers reach via the confirm flow.
_BASE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
# HTML pages add the anti-injection headers for the platform's only
# unauthenticated HTML route.
_HTML_HEADERS = {
    **_BASE_HEADERS,
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# The form page needs its own inline submit script (a native form cannot build the
# typed ``{"answer": {...}}`` JSON body) plus a same-origin fetch back to this
# callback URL. The CSP widens only to a constant inline ``script-src`` and a
# ``connect-src``/``form-action`` pinned to ``'self'`` — no external origins, no
# ``unsafe-eval`` — over the same locked-down ``default-src 'none'`` base.
_FORM_HTML_HEADERS = {
    **_BASE_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# Byte-constant pages: zero interpolation of any request-derived value. The
# confirm form posts to the SAME URL (empty action preserves the query string).
_CONFIRM_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>Confirm</title>\n'
    "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}"
    "button{font-size:1rem;padding:.6rem 1.4rem}</style></head><body>\n"
    "<h1>Confirm</h1>\n"
    "<p>Click confirm to submit your response.</p>\n"
    '<form method="post"><button type="submit">Confirm</button></form>\n'
    "</body></html>\n"
)
_DONE_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>Done</title>\n'
    "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}</style></head><body>\n"
    "<h1>Done</h1>\n"
    "<p>This interaction has already been answered.</p>\n"
    "</body></html>\n"
)
# Served on GET for a ticketed text/select question (channel delivery mints a
# ticket for every format): a value answer is required, so the page presents no
# form — the confirm button's empty-body POST could never succeed here.
_REPLY_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>Awaiting reply</title>\n'
    "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}</style></head><body>\n"
    "<h1>Awaiting your reply</h1>\n"
    "<p>Answer this question by replying on the channel where you received it.</p>\n"
    "</body></html>\n"
)

# The constant submit script for the form page: it reads the rendered fields,
# coerces number inputs to numbers and checkboxes to booleans, omits empty
# optional fields, and POSTs ``{"answer": {...}}`` as JSON to THIS same callback
# URL. On 200 (answered or already_answered) it swaps to a done state; on 400 it
# renders the door's own error text and lets the visitor retry. The script body
# is a constant — only the field markup above it is schema-derived (and escaped).
_FORM_SUBMIT_SCRIPT = """<script>
(function () {
  var form = document.getElementById('askform');
  var err = document.getElementById('err');
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    err.textContent = '';
    var answer = {};
    var fields = form.querySelectorAll('[data-field]');
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i];
      var name = el.getAttribute('data-field');
      var kind = el.getAttribute('data-kind');
      if (kind === 'boolean') {
        answer[name] = el.checked;
      } else if (kind === 'number') {
        if (el.value !== '') { answer[name] = Number(el.value); }
      } else {
        if (el.value !== '') { answer[name] = el.value; }
      }
    }
    fetch(window.location.href, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: answer })
    }).then(function (resp) {
      if (resp.status === 200) {
        document.body.innerHTML = '<h1>Done</h1><p>Your response has been submitted.</p>';
        return;
      }
      if (resp.status === 400) {
        resp.json().then(function (data) {
          err.textContent = (data && data.error) ? data.error : 'Invalid submission, please check your answers.';
        }).catch(function () {
          err.textContent = 'Invalid submission, please check your answers.';
        });
        return;
      }
      err.textContent = 'Submission failed, please try again.';
    }).catch(function () {
      err.textContent = 'Network error, please try again.';
    });
  });
})();
</script>
"""


class _FormRenderError(Exception):
    """Raised when a stored form question's schema cannot be rendered into a page —
    a schema outside the channel-deliverable subset (``form_schema``). ``ask_user``
    refuses such a schema before persisting, so a form record that reaches the GET
    door MUST render; failing to is a server bug (a record that bypassed
    ``ask_user``), so it surfaces as a loud 500 with a logged reason, never a blank
    or half-rendered page silently dropping fields."""


def _render_field(name: str, prop: dict[str, Any], is_required: bool) -> str:
    """Render one subset-validated schema property into an escaped form control:
    ``string`` (``enum`` -> ``<select>``, else text), ``boolean`` -> checkbox,
    ``integer``/``number`` -> number input. The property is pre-validated by
    ``channel_form_fields`` (the one subset definition), so its type is always a
    scalar and any ``enum`` is a non-empty list of strings; an unexpected type is a
    server bug and raises ``_FormRenderError``. ``data-field``/``data-kind`` drive
    the submit script's typed collection."""
    esc_name = html.escape(name, quote=True)
    esc_label = html.escape(str(prop.get("title") or name))
    req_attr = " required" if is_required else ""
    ptype = prop.get("type")
    if ptype == "string":
        enum = prop.get("enum")
        if enum is not None:
            # A leading blank option lets an optional select stay empty and forces a
            # required one (with the ``required`` attr) to a real choice on submit.
            parts = ['<option value="">—</option>']
            for choice in enum:
                text = str(choice)
                parts.append(f'<option value="{html.escape(text, quote=True)}">{html.escape(text)}</option>')
            control = f'<select data-field="{esc_name}" data-kind="string"{req_attr}>' + "".join(parts) + "</select>"
        else:
            control = f'<input data-field="{esc_name}" data-kind="string" type="text"{req_attr}>'
    elif ptype == "boolean":
        # A checkbox always submits a boolean (checked/unchecked), so the field is
        # always present — no ``required`` attribute, which would force it checked.
        control = f'<input data-field="{esc_name}" data-kind="boolean" type="checkbox">'
    elif ptype in ("integer", "number"):
        step = ' step="1"' if ptype == "integer" else ' step="any"'
        control = f'<input data-field="{esc_name}" data-kind="number" type="number"{step}{req_attr}>'
    else:
        raise _FormRenderError(f"form schema property {name!r} has unsupported type {ptype!r}")
    return f"<label>{esc_label}<br>{control}</label>"


def _render_form_page(format_payload: dict[str, Any] | None) -> str:
    """Render the schema-driven HTML form for a channel-delivered form question,
    over the SAME subset walk (``channel_form_fields``) that ``ask_user`` enforces
    at ask time. A schema outside the subset raises ``_FormRenderError``; the GET
    door maps that to a loud 500 (the server-bug backstop for a record that
    bypassed ``ask_user``)."""
    schema = (format_payload or {}).get("schema")
    try:
        fields = channel_form_fields(schema)
    except ValueError as exc:
        raise _FormRenderError(str(exc)) from exc
    rows = [_render_field(name, prop, is_required) for name, prop, is_required in fields]
    fields_html = "\n".join(rows)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><title>Respond</title>\n'
        "<style>body{font-family:system-ui,sans-serif;margin:3rem;max-width:40rem}"
        "label{display:block;margin:1rem 0}input,select{font-size:1rem;margin-top:.3rem}"
        "button{font-size:1rem;padding:.6rem 1.4rem;margin-top:1rem}"
        "#err{color:#b00020;margin-top:1rem}</style></head><body>\n"
        "<h1>Respond</h1>\n"
        '<form id="askform">\n'
        f"{fields_html}\n"
        '<div id="err" role="alert"></div>\n'
        '<button type="submit">Submit</button>\n'
        "</form>\n"
        f"{_FORM_SUBMIT_SCRIPT}"
        "</body></html>\n"
    )


# The free-form label recorded on an answer delivered through the callback door.
_EXTERNAL_ANSWERED_BY = "external-callback"

# A ``post_only`` (body-signature) verifier authenticates the raw body only, so an
# empty-body POST carries no signed answer — the query string is unauthenticated
# and must never supply the answer. The deny is a constant, request-independent
# message: the answer must ride the signed body.
_POST_ONLY_EMPTY_BODY_DENY = "verified callback requires a signed request body"

# Every GET that will not serve a page answers ONE constant body: whether the
# ticket is unknown, expired, pruned, or bound to a verifier-only question —
# answered or still live — must not be distinguishable from outside. A ticket
# holder would otherwise have a silent state oracle on a capability URL.
_CALLBACK_GET_NOT_FOUND = "Not Found"


class _PayloadTooLarge(Exception):
    """Raised when a request body or query string exceeds the configured cap."""


# -- shared helpers ----------------------------------------------------------


def _callback_json(payload: dict, status_code: int) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=dict(_BASE_HEADERS))


# Invalid JSON in an untrusted body converts to a loud 400; any exception outside
# this set is a server bug and propagates as a 500. RecursionError covers a
# deeply-nested body blowing up the parser; ``json.JSONDecodeError`` is a
# ``ValueError``.
_JSON_PARSE_ERRORS = (ValueError, RecursionError)


# -- SSE stream (route 1) ----------------------------------------------------


def _frame(event: str, data: dict) -> str:
    # ``json.dumps`` of the whole payload is what stops an attacker-supplied
    # answer (with newlines / ``data:`` sequences) from injecting extra frames.
    # Frame values are JSON-native by construction (they round-trip through the
    # contract models), so a serialization failure is a server bug and raises.
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_events(request: Request, store: InteractionStore, settings: InteractionsSettings, cursor: str):
    """Resolve the caller's isolation identity once. A RESTRICTED caller (owner claim
    present) sees ONLY interactions addressed to it: an ``add`` frame is filtered on
    the record's ``audience == <own id>``, and an ``answered``/``removed`` frame on
    the audience the store stamps into the event payload — so a terminal frame is
    filtered directly, with no record left to read. An UNRESTRICTED caller sees every
    frame (the operator inbox). This is a TAIL-ONLY stream: the pending set is served
    by the paged ``GET /api/interactions`` door, so the stream carries no historical
    backlog and no end-of-backlog marker — only the live add/answered/removed tail.
    ``cursor`` is the events-stream tail the route handler captured BEFORE returning the
    response, so any add published after the client has the response headers has an id
    past it and is guaranteed delivered (the generator body runs only once the response
    iterates).
    """
    _user_id, restricted_id = request_identity()
    restricted = restricted_id is not None

    # What follows is the NEVER-completing live tail. Exempt
    # this request from its serving generation's retire drain now: this is a plain Starlette
    # route on its own redis connection — a reload's ``aclose`` closes only the FastMCP
    # session-manager, so it does NOT sever this stream; the tail can never drain, so a retire
    # that waited on it would burn its whole budget and, on a fleet sibling, stall fleet-reload
    # convergence. The stream self-terminates on client disconnect. Only this long-lived stream
    # opts out; real requests stay counted and are drained normally.
    mark_current_request_drain_exempt()

    # Flush a no-op comment immediately, before the tail's first (blocking) XREAD: this
    # is the first body byte, so a fetch-reader client resolves the stream at connect and
    # runs its connect-time base refetch now — not up to a keepalive interval later (the
    # Firefox inbox-staleness fix; see `_CONNECT_FRAME`).
    yield _CONNECT_FRAME

    # The tail blocks ~15s per iteration; a dedicated fresh connection keeps it
    # off the shared pool the answer door needs. The socket read timeout is
    # stripped on this connection only — the keepalive XREAD blocks legitimately
    # for the keepalive window, which a blanket 5s read timeout would kill; the
    # outer wait_for below bounds a black-holed redis instead.
    tail_redis = settings.redis.model_copy(update={"socket_timeout": None})
    # Keepalive cadence is governed by a monotonic loop-clock DEADLINE, not by
    # whether a given XREAD returned events. Any global-stream event (including one
    # filtered out for a restricted caller) makes XREAD return early; tying the
    # keepalive to that would let a restricted caller infer other identities'
    # activity timing from keepalive jitter. Instead XREAD blocks only until the
    # next deadline, the keepalive fires when the deadline passes, and the deadline
    # resets only on a frame actually yielded to THIS caller — so the cadence is
    # identical whether or not other identities are active.
    next_keepalive = _now() + _KEEPALIVE_SECONDS
    async with client_ctx(RedisClient, tail_redis, fresh=True) as tail_conn:
        while True:
            if await request.is_disconnected():
                break
            block_seconds = max(0.0, next_keepalive - _now())
            try:
                result = await asyncio.wait_for(
                    tail_conn.xread({store.events_key: cursor}, block=max(1, int(block_seconds * 1000))),
                    timeout=block_seconds + settings.blocking_grace_seconds,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    "interactions SSE tail: redis XREAD did not return within the keepalive "
                    f"window + {settings.blocking_grace_seconds}s grace — connection presumed stalled"
                ) from exc
            yielded = False
            for _stream, messages in result or ():
                for message_id, data in messages:
                    cursor = as_str(message_id)
                    fields = {as_str(k): as_str(v) for k, v in data.items()}
                    event_type = fields.get("type")
                    interaction_id = fields.get("interaction_id")
                    group_id = fields.get("group_id")
                    if event_type is None or interaction_id is None or group_id is None:
                        # A malformed entry (partial XADD, older/newer schema,
                        # seeded frame) is skipped, never fatal — one bad event
                        # must not tear down the whole SSE tail.
                        logger.debug("skipping malformed stream event %s: missing required field", cursor)
                        continue
                    if event_type == ADD_EVENT:
                        state = await store.get_state(tail_conn, interaction_id)
                        # A state pruned/expired between the event and this read
                        # has nothing left to show — the add frame is skipped,
                        # matching the pending-only filter.
                        if state is None:
                            continue
                        if restricted and state.request.audience != restricted_id:
                            continue
                        yield _frame(ADD_EVENT, _add_data(state.request))
                        yielded = True
                    elif event_type in (ANSWERED_EVENT, REMOVED_EVENT):
                        # Filter directly on the audience the store stamped into the
                        # event: a restricted caller sees a terminal frame only for its
                        # OWN addressed interaction (an absent audience field is an
                        # unaddressed question, which a restricted caller never sees).
                        if restricted and fields.get("audience") != restricted_id:
                            continue
                        yield _frame(event_type, {"interaction_id": interaction_id, "group_id": group_id})
                        yielded = True
            # The keepalive is deadline-driven: it fires whenever the monotonic
            # deadline passes and no frame reached THIS caller this window — whether
            # XREAD returned nothing OR only events filtered out for a restricted
            # caller. A frame that DID reach the caller restarts the countdown (it
            # doubles as liveness); a window of only filtered events leaves the
            # deadline untouched, so the caller's cadence stays independent of other
            # identities' volume and the connection never goes silent.
            if yielded:
                next_keepalive = _now() + _KEEPALIVE_SECONDS
            elif _now() >= next_keepalive:
                yield ": keepalive\n\n"
                next_keepalive = _now() + _KEEPALIVE_SECONDS


@http_surface().custom_route(
    "/api/interactions/stream",
    methods=["GET"],
    summary="Stream the interactions inbox live tail",
    tags=["interactions"],
    response_model=None,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(401, 501),
        success_status=200,
    ),
    action="read",
)
async def stream(request: Request) -> Response:
    # OFF gate — BEFORE the StreamingResponse is constructed: an unconfigured store
    # answers a plain 501+code up front rather than sending 200 + SSE headers and
    # then dying mid-body when the generator reaches for an absent Redis.
    if not interactions_store_configured():
        return JSONResponse(
            {"error": INTERACTIONS_NOT_CONFIGURED_MESSAGE, "code": INTERACTIONS_NOT_CONFIGURED_CODE},
            status_code=501,
        )
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    # Capture the tail cursor on the pooled connection BEFORE the response is returned, so
    # a client that has received the response headers is guaranteed every later add: any
    # event published after this read has an id past the cursor. The connection is
    # released here — the never-completing tail below runs on its own dedicated
    # connection and must never pin the shared pool.
    async with client_ctx(RedisClient, settings.redis) as r:
        tail = await r.xrevrange(store.events_key, count=1)
        cursor = as_str(tail[0][0]) if tail else "0-0"
    return StreamingResponse(
        _stream_events(request, store, settings, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# -- served-media door — an UNAUTHENTICATED capability URL --------------------

# A stored-media id: 43 urlsafe-base64 chars (32 random bytes), the whole path
# segment. Anything else is a malformed request, answered 400 before any store read.
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}")


@http_surface().custom_route(
    "/api/interactions/media/{media_id}",
    methods=["GET"],
    summary="Serve interaction media stored by reference",
    tags=["interactions"],
    response_model=None,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 404),
        success_status=200,
    ),
)
async def media(request: Request) -> Response:
    # UNAUTHENTICATED: the media id IS the capability secret — a vendor fetches the
    # url from its own servers, a browser ``<img>`` from the inbox origin. A malformed
    # id is a 400; an unconfigured store answers the SAME uniform 404 as a miss (never
    # a 501 that would oracle the store's absence), matching the callback door.
    media_id = request.path_params["media_id"]
    if _MEDIA_ID_RE.fullmatch(media_id) is None:
        return JSONResponse({"error": "invalid media id"}, status_code=400)
    if not interactions_store_configured():
        return JSONResponse({"error": "not found"}, status_code=404)
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        found = await read_media(store, r, media_id)
        if found is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        mime, payload = found
        # The remaining lifetime bounds the client cache: the bytes vanish at the key's
        # TTL (extended to the owning group's horizon), so a cache must not outlive it.
        remaining_ttl = await r.ttl(store.media_key(media_id))
    max_age = remaining_ttl if remaining_ttl > 0 else 0
    return Response(
        payload,
        media_type=mime,
        headers={"Cache-Control": f"private, max-age={max_age}", "X-Content-Type-Options": "nosniff"},
    )


# -- paged pending-list door — an operation adapter --------------------------


async def _extract_page_window(request: Request) -> dict:
    """The ``?page=`` / ``?pageSize=`` window as the list door's flat arguments (a GET
    reads its parameters from the query string, never a body). A non-integer is a loud
    400 here; the operation range-checks the pair and caps the size."""
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("pageSize", "50")
    try:
        return {"page": int(page), "page_size": int(page_size)}
    except ValueError as exc:
        raise BadRequestError(f"page and pageSize must be integers: page={page!r} pageSize={page_size!r}") from exc


list_interactions = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_interactions_op),
    path="/api/interactions",
    method="GET",
    context_extractor=_extract_page_window,
    action="read",
)


# -- parked-interactions audit door — an operation adapter -------------------


async def _extract_pending_limit(request: Request) -> dict:
    """The ``?limit=`` slice as the audit door's flat argument (a GET reads its
    parameters from the query string, never a body). A non-integer is a loud 400 here;
    the operation clamps the value into range."""
    raw = request.query_params.get("limit")
    if raw is None:
        return {}
    try:
        return {"limit": int(raw)}
    except ValueError as exc:
        raise BadRequestError(f"limit must be an integer: limit={raw!r}") from exc


list_pending_interactions = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_pending_interactions_op),
    path="/api/interactions/pending",
    method="GET",
    context_extractor=_extract_pending_limit,
    action="read",
)


# -- human answer door — an operation adapter --------------------------------


async def _extract_answer(request: Request) -> dict:
    """Read + bound the human-answer body at the HTTP edge into the operation's flat
    ``answer`` argument. The byte cap (413), invalid JSON (400), and a missing
    ``answer`` key (400) are the same loud rejections the door has always answered —
    reproduced here so the operation receives an already-parsed answer value (the
    adapter's plain parse would yield 422)."""
    settings = interactions_settings()
    try:
        raw = await _read_bounded_body(request, settings.callback_max_body_bytes)
    except _PayloadTooLarge as exc:
        raise PayloadTooLargeError("payload too large") from exc
    try:
        body = json.loads(raw)
    except _JSON_PARSE_ERRORS as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict) or "answer" not in body:
        raise BadRequestError("body must contain 'answer'")
    return {"answer": body["answer"]}


answer = register_operation_route(
    tai42_app,
    operation_metadata_of(_answer_interaction_op),
    path="/api/interactions/{interaction_id}/answer",
    method="POST",
    context_extractor=_extract_answer,
    action="write",
)


# -- callback doors ----------------------------------------------------------


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the request body on ACTUAL bytes, never a client ``Content-Length``.
    Raise ``_PayloadTooLarge`` past ``cap`` before parsing — loud, never truncated.

    Idempotent: the fully-read body is cached on the request (Starlette's own
    ``_body`` slot) so a later re-read replays it instead of re-consuming the stream —
    the size guard and the callback handler can each read it once without a
    "stream consumed" error."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _PayloadTooLarge("request body exceeds the configured cap")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body


async def _callback_post_size_guard(request: Request, settings: InteractionsSettings) -> Response | None:
    """Enforce ``callback_max_body_bytes`` on BOTH the raw query string (the
    confirm-flow answer rides the URL) and the actual body bytes. Return the 413
    response when either exceeds the cap, or ``None`` when both are within it.

    Runs identically in the OFF gate and the configured POST path so an oversized
    POST answers the SAME 413 whether or not the store is configured — the public
    door leaks no configured-vs-off oracle through the size cap."""
    if len(request.url.query.encode()) > settings.callback_max_body_bytes:
        return _callback_json({"error": "payload too large"}, 413)
    try:
        await _read_bounded_body(request, settings.callback_max_body_bytes)
    except _PayloadTooLarge:
        return _callback_json({"error": "payload too large"}, 413)
    return None


def _params_to_answer(request: Request) -> dict:
    """The query params as the delivered answer: a single occurrence yields the
    scalar string (``?a=1`` -> ``{"a": "1"}``), a repeated key yields a list
    (``?tag=a&tag=b`` -> ``{"tag": ["a", "b"]}``) — never silent last-wins."""
    result: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


async def _record_callback_answer(
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    answer: Any,
    params: dict[str, str] | None = None,
) -> JSONResponse:
    """Atomically claim ``answer`` for the ticketed question. A lost race maps
    to the same idempotent 200 already_answered. ``params`` is the OPTIONAL channel enrichment the
    inbound-answer ladder forwarded alongside the answer (the answer-seam counterpart of a bridged
    turn's entry params); it rides the stored :class:`InteractionResponse.params` for the asking
    flow to read beside ``answer``. ``None`` keeps the envelope byte-identical to a plain answer."""
    response = InteractionResponse(
        interaction_id=interaction_id,
        answer=answer,
        answered_by=_EXTERNAL_ANSWERED_BY,
        answered_at=datetime.now(UTC),
        params=params,
    )
    # An async park enqueues its durable continuation-due record atomically with the
    # claim; a sync question passes no timing and enqueues nothing.
    due_ttl, due_first_attempt_at_ms = (
        continuation_due_timing(settings) if state.request.mode == "async" else (None, None)
    )
    claimed = await _claim_or_serialization_error(
        store,
        r,
        response,
        state.group_id,
        _reply_ttl(state.request),
        ticket=ticket,
        ticket_ttl=settings.idle_ttl_seconds,
        continuation_due_ttl=due_ttl,
        continuation_first_attempt_at_ms=due_first_attempt_at_ms,
    )
    if claimed is None:
        return _callback_json({"error": "answer payload could not be serialized"}, 400)
    if not claimed:
        return _callback_json({"data": {"status": "already_answered"}}, 200)
    # This door claimed the answer: fire an async park's stored continuation ONCE
    # (the shared post-claim seam both answer doors run).
    await fire_continuation_after_claim(r, store, state.request, answer)
    return _callback_json({"data": {"interaction_id": interaction_id, "status": "answered"}}, 200)


async def _claim_external(
    r: Any,
    store: InteractionStore,
    settings: InteractionsSettings,
    ticket: str,
    interaction_id: str,
    state: InteractionState,
    answer: Any,
) -> JSONResponse:
    """Validate (if a schema was declared), then atomically claim the answer."""
    schema = (state.request.format_payload or {}).get("schema")
    if schema is not None:
        mismatch = _schema_mismatch(answer, schema)
        if mismatch is not None:
            return _callback_json({"error": mismatch[0]}, 400)
    return await _record_callback_answer(r, store, settings, ticket, interaction_id, state, answer)


def _callback_verifier(state: InteractionState) -> dict | None:
    """The verifier binding stashed in the external ``format_payload`` (``{"name",
    "config"}``), or ``None`` for an unbound ticket-only external question."""
    binding = (state.request.format_payload or {}).get("verifier")
    return binding if isinstance(binding, dict) else None


async def _verify_callback(request: Request, raw: bytes, state: InteractionState) -> tuple[Response | None, bool]:
    """Run the question's bound verifier over the raw callback body. Return
    ``(deny_response, False)`` on any failure (nothing recorded, ticket
    unconsumed), or ``(None, post_only)`` when the question is unbound or
    verification passes — ``post_only`` tells the caller whether the verifier
    signs only the body (True for a body-signature verifier), so an empty-body
    POST must not draw its answer from the unauthenticated query string.

    The unbound question returns ``(None, False)`` so its ticket-only query-param
    path stays open; only a passing body-signature verifier returns
    ``(None, True)``.

    Fails CLOSED: a signature failure -> 401 (constant message); an unknown
    verifier name / missing secret env / verifier bug -> 500."""
    binding = _callback_verifier(state)
    if binding is None:
        return None, False
    name = binding.get("name")
    config = binding.get("config") or {}
    interaction_id = state.request.interaction_id
    if not isinstance(name, str):
        logger.error("callback verify: malformed verifier binding (no name) for interaction %s", interaction_id)
        return _callback_json({"error": "webhook verification error"}, 500), False
    try:
        verifier = tai42_app.webhook_verifiers.get(name)
    except Exception:
        logger.error("callback verify: no registered verifier %r for interaction %s", name, interaction_id)
        return _callback_json({"error": "webhook verification error"}, 500), False
    post_only = bool(getattr(verifier, "post_only", False))
    try:
        await verifier.verify(raw, request.headers, config)
    except WebhookVerificationError as exc:
        # Log the reason (never the raw body verbatim); return a constant message.
        logger.warning("callback verification failed for interaction %s: %s", interaction_id, exc)
        return _callback_json({"error": "webhook verification failed"}, 401), False
    except Exception:
        logger.error("callback verifier error for interaction %s", interaction_id, exc_info=True)
        return _callback_json({"error": "webhook verification error"}, 500), False
    return None, post_only


async def _callback_post(request: Request, r: Any, store: InteractionStore, settings: InteractionsSettings) -> Response:
    ticket = request.path_params["ticket"]

    # Size caps (query string + actual body bytes) come first — the SAME guard the OFF
    # gate runs, so an oversized POST answers 413 in both states. On success the body is
    # cached, so the re-read below replays it.
    oversized = await _callback_post_size_guard(request, settings)
    if oversized is not None:
        return oversized
    raw = await _read_bounded_body(request, settings.callback_max_body_bytes)

    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        # Uniform 404: same status and body whether the ticket never existed or
        # expired — no timing or body distinction.
        return _callback_json({"error": "not found"}, 404)
    state = await store.get_state(r, interaction_id)
    if state is None:
        # A cancel/timeout prune deletes the state while the ticket lives out its
        # TTL — the same uniform 404, never a None dereference.
        return _callback_json({"error": "not found"}, 404)
    # Verify the signed server-to-server answer over the RAW body BEFORE the
    # answered-state check, before parsing, and before recording. A bound door
    # authenticates before it reports anything about the ticket's state, so a
    # caller without the secret cannot tell a live bound question from an
    # answered one — both deny identically. Failure denies without consuming the
    # ticket or recording the answer — the idempotency window is untouched, so a
    # legitimate retry still works.
    denied, post_only = await _verify_callback(request, raw, state)
    if denied is not None:
        return denied

    if state.status == "answered":
        # Idempotent duplicate handling (incl. provider retries after success);
        # the ticket is never deleted and its TTL was refreshed on claim. A bound
        # question reaches this only after passing verification above.
        return _callback_json({"data": {"status": "already_answered"}}, 200)

    fmt = state.request.answer_format

    if fmt is not AnswerFormat.EXTERNAL:
        # A ticketed non-external question is channel-delivered: the plugin
        # forwards the human's reply as {"answer": <value>}. Validate it
        # against the STORED format (the authed door's exact rules) and record
        # the TYPED value. Query params never carry the answer here.
        value: Any
        answer_params: dict[str, str] | None = None
        if not raw:
            if fmt is AnswerFormat.CONFIRM:
                # The GET-confirm page's form POSTs an empty body — an
                # affirmative tap.
                value = True
            else:
                # text/select: an answer is required.
                return _callback_json({"error": "body must contain 'answer'"}, 400)
        else:
            try:
                parsed = json.loads(raw)
            except _JSON_PARSE_ERRORS:
                return _callback_json({"error": "body must be a JSON object"}, 400)
            if not isinstance(parsed, dict):
                return _callback_json({"error": "body must be a JSON object"}, 400)
            if "answer" not in parsed:
                return _callback_json({"error": "body must contain 'answer'"}, 400)
            value = parsed["answer"]
            # OPTIONAL channel enrichment the inbound-answer ladder forwarded alongside the
            # answer (the answer-seam counterpart of a bridged turn's entry params); validated
            # against the shared transport bounds and carried onto the stored response.
            if "params" in parsed:
                raw_params = parsed["params"]
                if not isinstance(raw_params, dict):
                    return _callback_json({"error": "params must be a JSON object"}, 400)
                try:
                    answer_params = validate_entry_params(raw_params)
                except ValueError as exc:
                    return _callback_json({"error": f"invalid params: {exc}"}, 400)
        try:
            validated = _validate_answer(state.request, value)
        except _AnswerInvalid as exc:
            # The failing field's dotted path rides as an optional ``field`` key so a
            # channel can pin the error on the right control; absent when unlocated.
            # ``retry_in_place`` is the door's policy signal to a correlated channel:
            # every current validation rejection is re-answerable in place (the live
            # ask stands and the guest can answer again), so it is always True here.
            body: dict[str, Any] = {"error": str(exc), "retry_in_place": True}
            if exc.field is not None:
                body["field"] = exc.field
            return _callback_json(body, 400)
        return await _record_callback_answer(
            r, store, settings, ticket, interaction_id, state, validated, params=answer_params
        )

    # EXTERNAL: verbatim payload semantics. Dispatch on the body FIRST —
    # empty-body branch first (``json.loads("")`` raises). Body wins: query
    # params alongside a JSON-object body are ignored as routing metadata
    # (webhook providers decorate the URL with their own params while POSTing
    # the event body).
    if not raw:
        # A body-signature verifier signs only the raw body; a replayed signature
        # over an empty body must never let ``?approved=true`` inject an answer.
        # Reject with a constant deny — nothing recorded, ticket unconsumed.
        if post_only:
            return _callback_json({"error": _POST_ONLY_EMPTY_BODY_DENY}, 400)
        answer = _params_to_answer(request)
    else:
        try:
            parsed = json.loads(raw)
        except _JSON_PARSE_ERRORS:
            return _callback_json({"error": "body must be a JSON object"}, 400)
        if not isinstance(parsed, dict):
            return _callback_json({"error": "body must be a JSON object"}, 400)
        answer = parsed

    return await _claim_external(r, store, settings, ticket, interaction_id, state, answer)


async def _callback_get(request: Request, r: Any, store: InteractionStore) -> Response:
    ticket = request.path_params["ticket"]
    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
    state = await store.get_state(r, interaction_id)
    if state is None:
        # State pruned while the ticket lives out its TTL tail — plain 404, never
        # a confirm page for a dead interaction.
        return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
    if _callback_verifier(state) is not None:
        # A verifier-bound question is server-to-server ONLY: the browser confirm
        # form posts an EMPTY body (answer in query params) and can never carry a
        # provider signature, so no page is ever served here — not a confirm page
        # while the question is live, and not a done page after it is answered.
        # Both states answer the same constant 404 every other non-page branch
        # sends: a caller must not learn from the response whether the ticket is
        # live, answered, or dead. The ticket for a bound question is never
        # exported to a human, so no legitimate reader loses anything.
        return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
    if state.status == "answered":
        return HTMLResponse(_DONE_PAGE, headers=_HTML_HEADERS)
    if state.request.answer_format is AnswerFormat.FORM:
        # A channel-delivered form needs a real answer surface: render its schema
        # server-side. An unrenderable schema on a form record is a server bug —
        # answer a loud 500 with a logged reason, never a blank/half-rendered page.
        try:
            page = _render_form_page(state.request.format_payload)
        except _FormRenderError:
            logger.exception("form callback GET: unrenderable schema for interaction %s", interaction_id)
            return PlainTextResponse("Internal Server Error", status_code=500, headers=_BASE_HEADERS)
        return HTMLResponse(page, headers=_FORM_HTML_HEADERS)
    if state.request.answer_format in (AnswerFormat.CONFIRM, AnswerFormat.EXTERNAL):
        # Only these formats map the confirm form's empty-body POST to an
        # answer (confirm -> True, external -> the query-param payload).
        return HTMLResponse(_CONFIRM_PAGE, headers=_HTML_HEADERS)
    # A ticketed text/select question needs a VALUE answer, which the channel
    # plugin forwards as a POST body — never a bare confirm tap, so no page
    # with an action that would be rejected.
    return HTMLResponse(_REPLY_PAGE, headers=_HTML_HEADERS)


@http_surface().custom_route(
    "/api/interactions/callback/{ticket}",
    methods=["GET", "POST"],
    summary="External interaction callback door",
    tags=["interactions"],
    response_model=None,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 401, 404, 413, 500),
        success_status=200,
    ),
)
async def callback(request: Request) -> Response:
    # Rate limiting for this public door lives in the app-level
    # ``RateLimitMiddleware``, registered at app construction so it is always on
    # (tunable/disable via ``TAI_RATE_LIMIT_*``); it runs ahead of this route for
    # the interactions-callback door family.
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)

    # OFF gate — BEFORE client_ctx. This is an UNAUTHENTICATED door, so an
    # unconfigured store must answer the door's OWN uniform 404 (indistinguishable
    # from an unknown/expired ticket), never a discriminable 501 that would oracle
    # the store's absence to an outsider.
    if not interactions_store_configured():
        if request.method == "GET":
            return PlainTextResponse(_CALLBACK_GET_NOT_FOUND, status_code=404, headers=_BASE_HEADERS)
        # A POST still runs the SAME size guard the configured path runs BEFORE the
        # uniform 404, so an oversized POST answers 413 identically in both states —
        # the size cap is not a configured-vs-off oracle on this public door.
        oversized = await _callback_post_size_guard(request, settings)
        if oversized is not None:
            return oversized
        return _callback_json({"error": "not found"}, 404)

    async with client_ctx(RedisClient, settings.redis) as r:
        if request.method == "GET":
            return await _callback_get(request, r, store)
        return await _callback_post(request, r, store, settings)
