"""HTTP surface for the hooks feature: the inbound event ingress plus the authed management doors.

The management doors are the ones the Studio's hooks UI consumes.

- ``POST|GET /universal_webhook/{topic}`` (PUBLIC ingress) — external systems
  deliver events here; the payload is parsed and kept as one hook run per
  registered hook on the topic before the door answers, so a 200 ``accepted``
  means the runs are kept; they then run, and are retried, asynchronously. A store
  fault keeping them is a loud 500 and keeps nothing. This is the most exposed door in the
  system, so the ingress is bounded (body + query cap -> 413), parses hostile
  content types safely (entity-expansion off), and answers with ``nosniff`` +
  ``no-store``. Rate limiting lives in the app-level ``RateLimitMiddleware``.
  A topic with NO verifier binding is OPEN BY DESIGN; bind a verifier to lock
  one. A bound topic verifies the RAW body BEFORE parsing; failure -> 401 with a
  constant message (no oracle), nothing dispatched. A
  body-signature (``post_only``) verifier rejects GET delivery — a GET door would
  sign an empty body while the real payload rides the URL unauthenticated. A
  passing verifier then declares its replay defense: a scheme carrying a stable
  per-delivery id yields a seen-set claim, and a delivery whose id was already
  claimed within the window is a REPLAY — answered with the idempotent
  ``already_seen`` 200, keeping nothing, so no bound hook re-fires. The claim is taken
  in the same store step that keeps the runs, so a delivery whose runs were not kept
  is never answered ``already_seen``. This ingress carries a discriminated status body
  (413/401/405/500/accepted/already_seen) with custom headers, so it stays a native handler.
- ``GET /api/hooks`` (AUTHED) — list registered hooks (``?topic=`` filters) plus
  the per-topic verifier bindings under ``data.topic_verifiers`` and, under
  ``data.trigger_auth``, how a topic's webhook ingress door authenticates its caller
  (derived from those live bindings, never stored) — keyed by every topic among the
  listed hooks plus every topic currently carrying a binding.
- ``POST /api/hooks`` (AUTHED) — register a hook from a ``HookRegister`` body.
- ``DELETE /api/hooks/{name}`` (AUTHED) — unregister a hook by name; a missing
  name is a loud 404.
- ``PUT /api/hooks/topics/{topic}/verifier`` (AUTHED, ``fenced``) — set/replace a
  topic's verifier binding; an unknown verifier name is rejected at bind time (400).
- ``DELETE /api/hooks/topics/{topic}/verifier`` (AUTHED, ``fenced``) — remove a
  binding; a missing binding is a loud 404.

  Both verifier doors are ADMIN-ONLY. A binding is the only authentication
  ``/universal_webhook/{topic}`` has, and the topic namespace has no owner: any
  ``hooks``-write holder could otherwise take the lock off a topic whose hooks fire
  under keys it could never bind, or replace that lock with one whose secret it
  chooses. Removing a lock and replacing one reach the same state, so they carry the
  same fence — gating only the unbind would leave the rebind as its bypass.
- ``GET|POST /trigger/{token}`` (PUBLIC) — the trigger-link door: resolve a minted
  token to a hook topic and dispatch the payload, exactly like the ingress door but
  hiding its topic and merging the link's stored ``tool_kwargs`` in BELOW each fired
  hook's own static ``tool_kwargs``, so the link fills only the arguments that hook's
  author left unpinned. Every miss is
  the uniform 404 (no oracle). A link minted ``require_api_key`` demands an
  authenticated principal beside the token — a token holder without one gets a 403,
  and a credential the authentication backend does not admit (invalid, disabled, or
  role-governed and refused at this door's method) never reaches this handler. The
  payload is kept as hook runs before the door answers (a store fault is a loud 500),
  and every attempt of each run is gated on the link's bound execution key; each fired
  hook's tool call is authorized against the HOOK's key.
- ``GET /api/hook-runs`` (AUTHED, ``read``) — list the kept hook runs
  (``?status=outstanding|failed``, ``?limit=``, ``?cursor=``) with their totals, never their
  payloads.
- ``POST /api/hook-runs/{id}/retry`` (AUTHED, ``write``) — re-queue a failed hook run.
- ``DELETE /api/hook-runs/{id}`` (AUTHED, ``write``) — discard a failed hook run.
- ``POST /api/hooks/trigger-links`` (AUTHED, ``write``) — mint a trigger link.
- ``GET /api/hooks/trigger-links`` (AUTHED, ``read``) — list trigger links.
- ``DELETE /api/hooks/trigger-links/{name}`` (AUTHED, ``write``) — revoke by name.

The management doors are thin adapters over the operations in
``tai42_skeleton.operations.hooks`` (the same manager the MCP hook-management tools
drive) — no hook logic lives here. Each management door's body is parsed and
structurally validated at the HTTP edge (a strict 400 surface) into the
operation's flat arguments; the operation owns the logical guards. Success bodies are ``{"data": ...}``; failures are
``{"error": "<message>"}``.
"""

from __future__ import annotations

import logging

from fastapi import Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, Response
from tai42_contract.app import DeclaredRouteMetadata, tai42_app
from tai42_contract.hooks import HookRegister, TopicVerifierBinding
from tai42_contract.webhooks import FreshnessWindow, SeenSetClaim
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body

from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.hooks.cache import get_hooks_manager
from tai42_skeleton.hooks.payload_parser import parse_any_payload
from tai42_skeleton.hooks.runs.models import DispatchKey, ReplayClaim
from tai42_skeleton.hooks.trigger_links import TriggerLinkError, resolve_trigger_token
from tai42_skeleton.middleware.rate_limit import SKELETON_DOOR_BUDGET
from tai42_skeleton.operations import (
    BadRequestError,
    operation_metadata_of,
    register_operation_route,
)
from tai42_skeleton.operations.adapter import read_json_object, request_with_body, validate_body
from tai42_skeleton.operations.hooks import TriggerLinkCreate
from tai42_skeleton.operations.hooks import create_trigger_link as _create_trigger_link_op
from tai42_skeleton.operations.hooks import delete_topic_verifier as _delete_topic_verifier_op
from tai42_skeleton.operations.hooks import delete_trigger_link as _delete_trigger_link_op
from tai42_skeleton.operations.hooks import discard_hook_run as _discard_hook_run_op
from tai42_skeleton.operations.hooks import list_hook_runs as _list_hook_runs_op
from tai42_skeleton.operations.hooks import list_hooks as _list_hooks_op
from tai42_skeleton.operations.hooks import list_trigger_links as _list_trigger_links_op
from tai42_skeleton.operations.hooks import list_verifiers as _list_verifiers_op
from tai42_skeleton.operations.hooks import register_hook as _register_hook_op
from tai42_skeleton.operations.hooks import retry_hook_run as _retry_hook_run_op
from tai42_skeleton.operations.hooks import set_topic_verifier as _set_topic_verifier_op
from tai42_skeleton.operations.hooks import unregister_hook as _unregister_hook_op
from tai42_skeleton.webhooks.settings import webhook_ingress_settings
from tai42_skeleton.webhooks.verify import VERIFY_ERROR, WebhookVerifyFailure, verify_webhook

logger = logging.getLogger(__name__)

# nosniff + no-store ride every ingress response: the door is public and its
# body reflects attacker-influenced content the caller should never cache or
# have content-sniffed.
_INGRESS_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}

# The refusal a token-holder gets on an api-key-authed trigger link they rang with no
# authenticated principal — actionable, and reachable only by a token holder.
_API_KEY_REQUIRED = "this trigger link requires an authenticated api key"

# The body of the 500 an ingress door answers when the event's runs could not be kept.
_ACCEPT_FAILED = "accept failed"


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=dict(_INGRESS_HEADERS))


def _ingress_json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=dict(_INGRESS_HEADERS))


def _sanitize_topic_for_log(topic: str) -> str:
    """Strip CR and LF so a crafted topic path param cannot forge extra log lines (log injection).

    Both are removed — a lone carriage return can rewrite a line on many terminals,
    not only a newline.
    """
    return topic.replace("\r", "").replace("\n", "")


# -- Inbound event ingress (PUBLIC, native) ----------------------------------


class WebhookIngressResult(BaseModel):
    """The webhook ingress door's raw (non-enveloped) 200 body.

    The delivery's disposition (``accepted`` for a delivery whose hook runs are kept,
    ``already_seen`` for an idempotent replay that kept nothing) and the topic it
    addressed.
    """

    status: str
    topic: str


@tai42_app.http.custom_route(
    "/universal_webhook/{topic}",
    methods=["POST", "GET"],
    summary="Public webhook ingress door for a topic",
    tags=["hooks"],
    response_model=WebhookIngressResult,
    enveloped=False,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=True,
        error_statuses=(400, 401, 405, 413, 500),
        success_status=200,
        rate_limit_budget=SKELETON_DOOR_BUDGET,
    ),
)
async def universal_webhook(request: Request) -> Response:
    """Ingest a public webhook delivery for ``{topic}``: keep one hook run per hook on it, then answer."""
    topic = request.path_params["topic"]
    logger.info("--- INCOMING EVENT ON TOPIC: %s ---", _sanitize_topic_for_log(topic))

    cap = webhook_ingress_settings().max_body_bytes
    # Payload rides the query string too (a GET/POST decorated URL), so cap it on
    # actual bytes exactly like the body.
    if len(request.url.query.encode()) > cap:
        return _error("payload too large", 413)
    try:
        raw = await read_bounded_body(request, cap)
    except RequestBodyTooLargeError:
        return _error("payload too large", 413)
    # The verifier runs over these exact raw bytes BEFORE any parse.

    manager = get_hooks_manager()
    binding = await manager.get_topic_verifier(topic)
    # A body-signature (``post_only``) verifier authenticates the raw body only;
    # its topic's dispatched payload must therefore exclude the unauthenticated
    # query string (else a captured signed delivery replays with appended params).
    strip_query = False
    claim: SeenSetClaim | None = None
    if binding is not None:
        # ``_verify_ingress`` verifies and reads the verifier's replay defense: it returns a
        # terminal response for a failed verification (401/405/500), or the delivery's
        # seen-set claim (``None`` for a freshness-window scheme) for the accept to take.
        verify_response, strip_query, claim = await _verify_ingress(request, raw, binding, topic)
        if verify_response is not None:
            return verify_response

    try:
        payload = await parse_any_payload(request_with_body(request, raw), include_query=not strip_query)
    except ValueError as e:
        return _ingress_json({"status": "rejected", "topic": topic, "error": str(e)}, status_code=400)

    replay_claim = None if claim is None else ReplayClaim(claim.key, claim.ttl_seconds)
    try:
        result = await manager.on_event(topic=topic, payload=payload, replay_claim=replay_claim)
    except Exception:
        # Nothing was kept and no delivery id was claimed, so the sender's redelivery is
        # processed fresh. Loud 500, logged.
        logger.error("webhook accept failed for topic %s", _sanitize_topic_for_log(topic), exc_info=True)
        return _error(VERIFY_ERROR if claim is not None else _ACCEPT_FAILED, 500)
    if result.status == "already_seen":
        # A delivery id already claimed within the window: a replay (or the sender's own
        # redelivery). Refused loudly in the log, answered with the idempotent already-seen
        # 200: nothing is kept, no hook re-fires.
        logger.warning("webhook replay refused for topic %s (delivery id already seen)", _sanitize_topic_for_log(topic))
        return _ingress_json({"status": "already_seen", "topic": topic})
    return _ingress_json({"status": "accepted", "topic": topic})


class TriggerResult(BaseModel):
    """The trigger-link door's raw (non-enveloped) 200 body: the delivery's disposition.

    It carries NO topic — a trigger link hides its topic from the URL holder.
    """

    status: str


@tai42_app.http.custom_route(
    "/trigger/{token}",
    methods=["POST", "GET"],
    summary="Public trigger-link door",
    tags=["hooks"],
    response_model=TriggerResult,
    enveloped=False,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=True,
        error_statuses=(400, 403, 404, 413, 500, 501),
        success_status=200,
        rate_limit_budget=SKELETON_DOOR_BUDGET,
    ),
)
async def trigger_link(request: Request) -> Response:
    """Fire a hook topic from a minted, token-bearing PUBLIC URL.

    A QR scan is a GET; POST comes free for curl symmetry — an anonymous token holder
    reaches both alike, while a caller that PRESENTS a credential is additionally
    subject to the ordinary route gate, which for a ROLE-governed principal derives
    ``read`` from GET and ``write`` from POST — the governing policy is the OWNER's for
    an owned key, so a key escapes that pass exactly when its governing policy is admin
    or carries no role pointer. The token is the capability — whoever holds the URL
    fires the topic's registered hooks.

    Every miss is the SAME 404 ``"unknown or expired trigger link"`` (unknown /
    expired / revoked / verifier-bound / in-memory-mode are deliberately
    indistinguishable — no oracle). A link minted ``require_api_key`` answers 403 to a
    token holder presenting no authenticated principal. The token is resolved BEFORE
    the payload is parsed, so an unknown token 404s without ever reaching a parse-400.
    The accepted response carries NO topic (a trigger link hides its topic from the URL
    holder); the payload rides query/body under the ingress byte cap; and the link's
    stored ``tool_kwargs`` merge into each fired hook's input BELOW that hook's own
    static ``tool_kwargs``, filling only the arguments its author left unpinned — a
    link can never restate an argument the hook pinned. The accepted response means the
    topic's hook runs are kept; a store fault keeping them is a loud 500. Every attempt of
    each run binds the link's execution key, so a revoked link key fails the run, recorded
    and notified. Every route-emitted response (accepted / 404 / 403 / 400 / 413 / 500)
    carries ``nosniff`` + ``no-store`` — a capability-URL response must never be cached.
    """
    token = request.path_params["token"]

    cap = webhook_ingress_settings().max_body_bytes
    if len(request.url.query.encode()) > cap:
        return _error("payload too large", 413)
    try:
        raw = await read_bounded_body(request, cap)
    except RequestBodyTooLargeError:
        return _error("payload too large", 413)

    # Resolve first: an unknown/expired/revoked/verifier-bound/in-memory token is the
    # uniform 404 before any parse work.
    try:
        resolved = await resolve_trigger_token(token)
    except TriggerLinkError as exc:
        return _error(exc.message, exc.status)

    # A principal ON TOP of the token. Only a token holder reaches this branch, so the 403
    # is actionable for them and invisible to everyone else (uniform 404 above).
    if resolved.require_api_key and not _authenticated_caller(request):
        # Logged: the resolver's preceding line reports only that the token resolved,
        # which reads as a successful fire without this.
        logger.warning("hooks: trigger door refused topic=%r cause=api-key-required", resolved.topic)
        return _error(_API_KEY_REQUIRED, 403)

    try:
        payload = await parse_any_payload(request_with_body(request, raw), include_query=True)
    except ValueError as e:
        # No topic echoed on the rejection — the link hides its topic.
        return _ingress_json({"status": "rejected", "error": str(e)}, status_code=400)

    try:
        await get_hooks_manager().on_event(
            topic=resolved.topic,
            payload=payload,
            tool_kwargs_override=resolved.tool_kwargs,
            dispatch_key=DispatchKey(resolved.execution_key, resolved.execution_key_fingerprint),
        )
    except Exception:
        # Nothing was kept. Loud 500, logged; the topic stays out of the body.
        logger.error("trigger accept failed for topic %r", resolved.topic, exc_info=True)
        return _error(_ACCEPT_FAILED, 500)
    return _ingress_json({"status": "accepted"})


def _authenticated_caller(request: Request) -> bool:
    """Whether the request carries a valid authenticated principal.

    With access control DISABLED, unconditionally ``True``.
    The door is registered ``authed=False`` (a static flag a per-record requirement cannot
    flip), but the authentication backend still runs and has already denied every
    credential it does not admit — so ``request.user`` is the decision here.
    """
    if not access_control_settings().enable:
        return True
    return bool(request.user.is_authenticated)


async def _verify_ingress(
    request: Request, raw: bytes, binding: TopicVerifierBinding, topic: str
) -> tuple[Response | None, bool, SeenSetClaim | None]:
    """Verify the topic's bound verifier over the raw body, then read its replay defense.

    Return ``(terminal_response, False, None)`` when the door must answer without keeping
    anything (a verification failure), or ``(None, post_only, claim)`` for a verified
    delivery: ``post_only`` tells the caller whether to drop the unauthenticated query string
    (True for a body-signature verifier), and ``claim`` is the delivery's seen-set claim the
    accept takes together with keeping the runs (``None`` for a freshness-window scheme,
    whose verify already stale-rejects).

    Fails CLOSED on every failure path (:func:`verify_webhook`: 401 / 405 / 500 with the
    constant bodies); an unrecognized replay declaration is a 500.
    """
    safe_topic = _sanitize_topic_for_log(topic)
    try:
        verdict = await verify_webhook(
            binding,
            raw,
            request.headers,
            method=request.method,
            subject=f"topic {safe_topic}",
            with_replay_defense=True,
        )
    except WebhookVerifyFailure as exc:
        return _error(exc.message, exc.status), False, None
    defense = verdict.replay_defense
    if isinstance(defense, SeenSetClaim):
        return None, verdict.post_only, defense
    if not isinstance(defense, FreshnessWindow):
        # ANY value other than the two known forms is an unrecognized replay declaration:
        # never keep it undefended. Fail CLOSED with a loud 500, logged — the same posture
        # as a verifier bug.
        logger.error("webhook verify: unrecognized replay defense %r for topic %s", type(defense), safe_topic)
        return _error(VERIFY_ERROR, 500), False, None
    return None, verdict.post_only, None


# -- Hook management (AUTHED) — HTTP-edge extractors --------------------------


async def _extract_list_query(request: Request) -> dict:
    """The optional ``?topic=`` filter as the operation's flat ``topic`` argument.

    A GET reads its parameters from the query string, never a body.
    """
    return {"topic": request.query_params.get("topic")}


async def _extract_hook_runs_query(request: Request) -> dict:
    """The ``?status=``, ``?limit=`` and ``?cursor=`` of the hook-runs listing as the operation's arguments.

    A ``limit`` that is not an integer is a 400 here; the operation owns the range check.
    """
    ctx: dict = {"status": request.query_params.get("status"), "cursor": request.query_params.get("cursor")}
    raw_limit = request.query_params.get("limit")
    if raw_limit is not None:
        try:
            ctx["limit"] = int(raw_limit)
        except ValueError as exc:
            raise BadRequestError("'limit' must be an integer") from exc
    return ctx


async def _extract_hook_params(request: Request) -> dict:
    """Parse + validate the client-facing hook body into the operation's flat fields.

    Rejects a malformed body before the operation runs (the adapter's plain parse would
    yield 422; this yields an explicit 400 surface).
    Validated against ``HookRegister``, which carries no ``execution_key_fingerprint`` —
    the operation derives that server-side; a client-set one would pin an authorization
    anchor the server never verified.
    """
    params = validate_body(HookRegister, await read_json_object(request), "hook params")
    flat = params.model_dump()
    # ``model_dump`` lowers the binding to a plain dict, but the operation's
    # ``state_binding`` parameter (and its attach-on-use) needs the parsed model — pass the
    # instance so a body binding is not dropped/mishandled at the route edge.
    flat["state_binding"] = params.state_binding
    return flat


async def _extract_trigger_link_params(request: Request) -> dict:
    """Parse + validate the ``TriggerLinkCreate`` body into the operation's flat fields.

    Rejects a malformed body with an explicit 400 (the adapter's plain parse would
    yield 422; the ttl contract demands 400 for an absent/invalid ``ttl_seconds``).
    """
    return validate_body(TriggerLinkCreate, await read_json_object(request), "trigger link params").model_dump()


async def _extract_binding(request: Request) -> dict:
    """Parse + validate a PUT binding body through :class:`TopicVerifierBinding` into the operation's args.

    The unknown-verifier check is the operation's, so the projected tool and CLI carry
    it too.
    """
    binding = validate_body(TopicVerifierBinding, await read_json_object(request), "verifier binding")
    return {"verifier": binding.verifier, "config": binding.config}


list_hooks = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_hooks_op),
    path="/api/hooks",
    method="GET",
    context_extractor=_extract_list_query,
    action="read",
)

register_hook = register_operation_route(
    tai42_app,
    operation_metadata_of(_register_hook_op),
    path="/api/hooks",
    method="POST",
    context_extractor=_extract_hook_params,
    action="write",
)

list_verifiers = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_verifiers_op),
    path="/api/hooks/verifiers",
    method="GET",
    action="read",
)

unregister_hook = register_operation_route(
    tai42_app,
    operation_metadata_of(_unregister_hook_op),
    path="/api/hooks/{name}",
    method="DELETE",
    action="write",
)

set_topic_verifier = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_topic_verifier_op),
    path="/api/hooks/topics/{topic}/verifier",
    method="PUT",
    context_extractor=_extract_binding,
    action="fenced",
)

delete_topic_verifier = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_topic_verifier_op),
    path="/api/hooks/topics/{topic}/verifier",
    method="DELETE",
    action="fenced",
)

create_trigger_link = register_operation_route(
    tai42_app,
    operation_metadata_of(_create_trigger_link_op),
    path="/api/hooks/trigger-links",
    method="POST",
    context_extractor=_extract_trigger_link_params,
    action="write",
)

list_trigger_links = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_trigger_links_op),
    path="/api/hooks/trigger-links",
    method="GET",
    action="read",
)

delete_trigger_link = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_trigger_link_op),
    path="/api/hooks/trigger-links/{name}",
    method="DELETE",
    action="write",
)

list_hook_runs = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_hook_runs_op),
    path="/api/hook-runs",
    method="GET",
    context_extractor=_extract_hook_runs_query,
    action="read",
)

retry_hook_run = register_operation_route(
    tai42_app,
    operation_metadata_of(_retry_hook_run_op),
    path="/api/hook-runs/{id}/retry",
    method="POST",
    action="write",
)

discard_hook_run = register_operation_route(
    tai42_app,
    operation_metadata_of(_discard_hook_run_op),
    path="/api/hook-runs/{id}",
    method="DELETE",
    action="write",
)
