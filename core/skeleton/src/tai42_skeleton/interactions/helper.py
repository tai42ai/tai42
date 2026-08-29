"""The author-facing ``ask_user`` surface — the ``AskUser`` contract impl.

Engine-agnostic: it reads no engine context and depends on nothing the engine
threads. Each call generates its own ``interaction_id`` and an optional caller
``group_id`` (uuid4 when absent) and persists the question to Redis. In
``mode="sync"`` it then blocks on a per-interaction reply channel until the answer
returns or the timeout budget elapses (loud ``InteractionTimeoutError`` — never a
silent default); in ``mode="async"`` it PARKS instead — returning a
``SuspendedInteraction`` at once — and a later answer/expiry resumes work out of
band.

The ``external`` answer format acts on an EXTERNAL surface (sign, approve, pay):
the caller blocks exactly as for any other format while the external system
delivers the answer through a public callback door. ``link`` supplies that
surface — a template carrying ``{callback_url}`` or a callable that builds the
external resource from the callback URL and returns its final URL.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from tai42_contract.app import tai42_app
from tai42_contract.channels import Channel, ChannelDelivery, ChannelDeliveryError
from tai42_contract.errors import ErrorKind
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    MediaItem,
    SuspendedInteraction,
    check_ask_timing,
    get_park_completion,
    get_resume_continuation_tool,
)
from tai42_contract.secrets import SecretValue
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import require

from tai42_skeleton.access_control.user import clamp_write_audience
from tai42_skeleton.interactions.form_schema import validate_channel_form_schema
from tai42_skeleton.interactions.media import substitute_media
from tai42_skeleton.interactions.origin import get_interaction_origin
from tai42_skeleton.interactions.settings import InteractionsSettings, interactions_settings
from tai42_skeleton.interactions.store import InteractionStore, PruneResult
from tai42_skeleton.tools.turn_budget import mark_parked_question

logger = logging.getLogger(__name__)

_CALLBACK_PLACEHOLDER = "{callback_url}"

# The platform-event topic emitted when a channel delivery of a question is
# TERMINALLY abandoned (retries exhausted / non-retryable / no budget left). Core
# states the fact; a deployment wires a hook (topic -> a tool such as notify_user, a
# ticket) in config to decide what an operator sees. It RIDES ALONGSIDE the unchanged
# raise that propagates the failure — it never replaces the error path.
DELIVERY_FAILED_EVENT_TOPIC = "interactions_delivery_failed"


class InteractionTimeoutError(Exception):
    """Raised when ``ask_user`` gets no answer within its timeout budget."""

    # No answer arrived within the timeout budget.
    __tai_error_kind__ = ErrorKind.TIMED_OUT


class InteractionLimitError(Exception):
    """Raised when a new ``ask_user`` call is refused because too many questions
    are already open (the ``max_concurrent`` guard)."""

    # Judgment call: the open-question ceiling is a saturated resource, so the ask is
    # refused for now — UNAVAILABLE (a temporary refusal), not a caller BAD_INPUT.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


def _normalize_schema(schema: type[BaseModel] | dict[str, Any]) -> dict:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return schema
    raise ValueError("schema must be a pydantic model or a JSON-schema dict")


def _build_payload(
    answer_format: AnswerFormat,
    options: list[str] | None,
    schema: type[BaseModel] | dict[str, Any] | None,
    url: str | None = None,
    verifier: dict[str, Any] | None = None,
) -> dict | None:
    if answer_format is AnswerFormat.SELECT:
        if not options:
            raise ValueError("answer_format 'select' requires options")
        return {"options": options}
    if answer_format is AnswerFormat.FORM:
        if schema is None:
            raise ValueError("answer_format 'form' requires a schema")
        return {"schema": _normalize_schema(schema)}
    if answer_format is AnswerFormat.EXTERNAL:
        # The URL exists only after the link is resolved, so this branch is called
        # after that step; schema (optional here) validates the callback payload.
        # A ``verifier`` (``{"name", "config"}``) rides the payload server-side so
        # the callback route can authenticate the signed server-to-server answer;
        # the client-facing serialization strips it (see ``routers.interactions``).
        payload: dict[str, Any] = {"url": url, **({"schema": _normalize_schema(schema)} if schema is not None else {})}
        if verifier is not None:
            payload["verifier"] = verifier
        return payload
    if answer_format is AnswerFormat.TEXT and options:
        # TEXT suggested replies: unlike SELECT (a constrained answer set), these are
        # optional pre-filled answers a human MAY tap — a tapped option submits its own
        # text as the free-text answer, which validates as any string. Stored so the inbox
        # can render the chips and the channel delivery frame carries them alike.
        return {"options": options}
    return None


async def _resolve_link(link: str | Callable[[str], Awaitable[str]], callback_url: str) -> str:
    """Turn the ``link`` argument into the final external URL the human visits."""
    if isinstance(link, str):
        if _CALLBACK_PLACEHOLDER not in link:
            raise ValueError(f"template link must contain {_CALLBACK_PLACEHOLDER}")
        # ``replace`` not ``format``: other braces in a real URL must survive.
        return link.replace(_CALLBACK_PLACEHOLDER, callback_url)
    # Callable flavor: it creates the external resource and returns its URL. An
    # exception from the builder propagates unchanged — nothing is persisted yet.
    final = await link(callback_url)
    if not isinstance(final, str) or not final.startswith(("http://", "https://")):
        raise ValueError(f"link builder must return an http(s) URL, got {final!r}")
    return final


def _validate_verifier(verifier: Any) -> None:
    """Reject a malformed or unknown ``verifier`` at ask-time, before any state is
    written. It must be a dict carrying a non-empty ``name`` that resolves against
    the registered webhook verifiers. A non-dict (or a typo'd/unregistered name)
    would otherwise slip through as an unrecognised binding at the callback door
    and silently degrade the question to an open, unverified one — so this is a
    hard guard (raise), never a soft ignore."""
    name = verifier.get("name") if isinstance(verifier, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError("verifier must be a dict with a non-empty 'name'")
    try:
        tai42_app.webhook_verifiers.get(name)
    except Exception as exc:
        raise ValueError(f"unknown webhook verifier: {name!r}") from exc


def _validate_channel(channel: Any) -> Channel:
    """Reject a malformed or unknown ``channel`` at ask-time, before any state
    is written. It must be a non-empty string naming a registered channel — an
    unknown name would otherwise persist a question no deliverer can ever push
    to a human, leaving the caller blocked until timeout. A hard guard (raise),
    never a soft ignore. Returns the resolved channel object; delivery reuses
    this exact validated instance, so a registry change between validation and
    delivery can never surface as a post-persist lookup failure."""
    if not isinstance(channel, str) or not channel:
        raise ValueError("channel must be a non-empty string")
    try:
        return tai42_app.channels.get(channel)
    except KeyError as exc:
        raise ValueError(f"unknown channel: {channel!r}") from exc


def _retry_delay(exc: BaseException, attempt: int, remaining: float, settings: InteractionsSettings) -> float | None:
    """Seconds to wait before re-attempting a failed channel delivery, or ``None``
    when it must not be retried: a non-transient (or non-delivery) failure, the
    attempt budget spent, or too little of the ask's budget left for the wait
    itself. The delay is the exponential backoff off the configured base,
    widened to the medium's own ``retry_after`` when it asked for longer."""
    if not isinstance(exc, ChannelDeliveryError) or not exc.retryable:
        return None
    if attempt >= settings.delivery_max_attempts:
        return None
    delay = max(exc.retry_after or 0.0, settings.delivery_retry_backoff_seconds * 2 ** (attempt - 1))
    return delay if delay < remaining else None


async def _prune(
    settings: InteractionsSettings, store: InteractionStore, interaction_id: str, group_id: str
) -> PruneResult:
    """Prune an abandoned question on its OWN connection — never the cancelled
    BLPOP connection, which is not safely reusable for a WATCH/MULTI. Returns
    ``prune_pending``'s result: ``"pruned"`` when it pruned, ``"answered"`` when an
    answer was already recorded, ``"gone"`` when the state key was already
    missing/expired."""
    async with client_ctx(RedisClient, settings.redis) as conn:
        return await store.prune_pending(conn, interaction_id, group_id)


async def _emit_delivery_failed(*, channel: str, interaction_id: str, recipient: str | None, error: str) -> None:
    """Emit the ``interactions_delivery_failed`` platform event ONCE when a channel
    delivery of a question has been TERMINALLY abandoned — the question pruned, nothing
    answered — alongside the unchanged raise that propagates the failure.

    Core states the fact; a deployment wires a hook on this topic (e.g. a ``notify_user``
    tool, a ticket) in config to decide what an operator sees. Best-effort: a
    hooks-manager failure is logged and swallowed so the event can never turn a loud
    delivery failure into a different error — the raise path stays exactly as it was.
    """
    # Local import: reach the hooks-manager accessor only when emitting, mirroring the
    # inbound ladder's pattern (keeps a module-load import edge out of this helper and
    # avoids an import cycle across packages).
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {
        "channel": channel,
        "interaction_id": interaction_id,
        "recipient": recipient,
        "error": error,
    }
    try:
        await get_hooks_manager().on_event(topic=DELIVERY_FAILED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "ask_user: failed to emit %r for the abandoned delivery on channel %r interaction %s",
            DELIVERY_FAILED_EVENT_TOPIC,
            channel,
            interaction_id,
            exc_info=True,
        )


# The park-completion context key a tool/flow route target binds the delivery thread under
# (see ``conversations.turn._run_tool_turn``'s ``set_park_completion``). Kept as a named
# constant so the read here and the turn-layer write name the same field.
_PARK_COMPLETION_THREAD_KEY = "delivery_thread_id"


def _bound_park_thread_id() -> str | None:
    """The conversation thread this async park belongs to, or ``None`` when none is bound
    (a background tool run, a direct/agent-less ask). Two turn-layer bindings expose it and
    this reads whichever is set, staying engine-agnostic (it names no engine, only the
    generic bindings):

    * a TOOL/flow route target binds the thread as the park-completion context's
      ``delivery_thread_id`` (``conversations.turn._run_tool_turn``), read here off
      :func:`get_park_completion`;
    * an AGENT route target runs inside the bridge turn context that carries the thread
      (``conversations.turn._run_agent_turn``), read here off ``current_bridge_turn``.

    Never fabricates a thread id — an unbound park indexes nothing. Both bindings are set only
    on a LIVE turn; a park raised OUTSIDE one — a background tool run, or a re-park during an
    out-of-band resume drive (which delivers via ``deliver_*_completion`` without re-wrapping
    the turn) — is unbound and so is not thread-indexed / not cascade-cancellable. A known,
    non-regressing boundary (nothing was indexed before this index existed); closing it would
    mean the resume drive re-establishing the thread binding, left to a follow-up."""
    _completion_tool, completion_ctx = get_park_completion()
    if completion_ctx is not None:
        candidate = completion_ctx.get(_PARK_COMPLETION_THREAD_KEY)
        if isinstance(candidate, str) and candidate:
            return candidate
    # Function-local, mirroring the ``get_execution_identity`` import below: a module-level
    # edge from interactions into conversations would couple two peer packages at import time.
    from tai42_skeleton.conversations.turn_context import current_bridge_turn

    bridge = current_bridge_turn()
    if bridge is not None:
        return bridge.thread_id
    return None


async def cancel_parks_for_thread(thread_id: str) -> list[str]:
    """Cancel every async ``ask_user`` park bound to ``thread_id`` — the entry point a
    conversation thread/person/route delete calls so a parked question the deletion would
    orphan is torn down (via the store's status-gated ``prune_pending``, firing NO
    continuation) instead of lingering muted until its expiry deadline. Runs on its own
    connection, like :func:`_prune`. A no-op when the interactions store is unconfigured
    (nothing could have been parked) or the thread holds no parks. Idempotent — safe to
    re-run under a delete's retry. Returns the cancelled interaction ids."""
    settings = interactions_settings()
    if not settings.redis.redis_url:
        # Interactions off: no park could ever have been persisted, so there is nothing
        # to cancel (and no Redis to reach for). The delete op proceeds unaffected.
        return []
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as conn:
        return await store.cancel_thread_parks(conn, thread_id)


async def ask_user(
    question: str,
    *,
    answer_format: str = "text",
    options: list[str] | None = None,
    schema: type[BaseModel] | dict[str, Any] | None = None,
    group_id: str | None = None,
    timeout: float | None = None,
    link: str | Callable[[str], Awaitable[str]] | None = None,
    verifier: dict[str, Any] | None = None,
    channel: str | None = None,
    recipient: str | None = None,
    sensitive: bool = False,
    audience: str | None = None,
    media: list[MediaItem | dict[str, Any]] | None = None,
    mode: Literal["sync", "async"] = "sync",
    expiry_at: datetime | None = None,
) -> Any:
    """Ask a human ``question``: in ``mode="sync"`` block until the answer returns;
    in ``mode="async"`` park the caller and return a ``SuspendedInteraction``
    immediately.

    Returns the typed answer per ``answer_format`` (text->str, confirm->bool,
    select->chosen value, form->validated dict, external->the callback payload).
    Raises ``InteractionTimeoutError`` on expiry, ``InteractionLimitError`` when
    the ``max_concurrent`` guard trips, ``ValueError`` for a bad format/argument
    combination or a blank ``audience``, ``CrossIdentityAudienceError`` when a
    RESTRICTED caller addresses another identity (a loud cross-identity authorization
    denial), and ``RuntimeError`` when an external question is asked without
    ``INTERACTIONS_PUBLIC_BASE_URL``. Invalid ``media`` raises
    ``pydantic.ValidationError`` when the ``InteractionRequest`` is built, before
    any state is written.

    ``link`` is required for ``answer_format="external"`` (unless a ``channel``
    delivers the question) and forbidden otherwise.

    ``verifier`` (``{"name", "config"}``) binds a registered webhook verifier to
    the external callback so the signed server-to-server answer is authenticated
    before it is recorded; it is only valid with ``answer_format="external"`` (a
    verifier is meaningless without the external callback route). It is stashed
    server-side in the ``format_payload`` and stripped from the client frame.

    ``sensitive`` marks the answer body as not-to-be-persisted AND wraps the
    returned answer in a ``SecretValue``: the caller reaches the real answer only
    through ``reveal()`` (its repr and JSON dump refuse to expose it), while the
    durable answered record keeps only the status (no response body). Use it for
    credentials or personal data.

    ``channel`` names a registered channel that delivers the question to a human
    on an external medium; ``None`` keeps the default Studio-inbox-only surface.
    A set channel forces the ticket + callback-URL mint for EVERY answer format
    (the channel bridges the reply back through the public callback door),
    forbids ``link`` and ``verifier`` (the channel owns delivery, and its
    forward is unsigned). ``answer_format="form"`` is delivered only over a
    channel that advertises ``supports_form_delivery``; a channel without the
    flag refuses the form loudly, naming the channel. A channel form's ``schema``
    must fall in the channel-deliverable subset (it is answered on the
    server-rendered callback page): root ``{"type": "object"}`` with a non-empty
    ``properties`` map; every property a scalar
    ``string``/``boolean``/``integer``/``number``; ``enum`` only on a ``string``
    property, a non-empty list of strings; a ``required`` list naming only
    declared properties. A schema outside that shared subset raises ``ValueError``
    naming the offending property, before any state is written (a non-channel form
    keeps full schema freedom). On TOP of the shared subset, the named channel's
    OPTIONAL ``validate_form_schema`` hook enforces its own ask-time-knowable
    limits (reserved property names, per-medium caps, question-text caps) over the
    schema AND the question text, also raising ``ValueError`` before any state is
    written — so a question or schema the channel could never render is refused up
    front, never persisted only to fail at delivery. An unknown name raises ``ValueError``
    before any state is written. The timeout budget bounds
    the WHOLE ask — the delivery attempts, their backoff sleeps, AND the answer
    wait together — so delivery time shrinks the answer wait, and a delivery phase
    that consumes the whole budget leaves no wait and times out. A delivery failure
    the channel typed as ``retryable`` is re-attempted (``delivery_max_attempts``,
    exponential backoff, all within that one budget); any other failure, and the
    last retryable one, prunes the question and re-raises (``ChannelDeliveryError``
    for a delivery failure, including a deliver call that does not return within the
    ask's timeout budget) — unless the reply already landed first, in which case the
    recorded answer is returned.

    ``recipient`` is an OPTIONAL per-call address (chat id, phone number, ...)
    carried to the named channel, which validates it against its operator
    allowlist — an unlisted address makes the delivery fail loudly; omitted,
    the channel sends to its operator-configured default recipient. Nothing is
    resolved or validated here beyond presence and non-emptiness (a set value
    must be a non-blank string): the plugin owns the allowlist. ``recipient``
    is forbidden when ``channel`` is ``None`` (an address is meaningless
    without a channel to send on).

    ``audience`` is the identity (a user_id) the question is addressed to:
    a restricted identity sees and answers ONLY questions addressed to it, while an
    unrestricted operator sees and may answer everything. Leave it unset for an
    operator/broadcast question. It is the isolation axis — a WHO, distinct from
    ``recipient`` (a channel delivery address — a WHERE) — and the two may be set
    together (address the question to identity A AND deliver it over a channel).

    ``media`` is optional display content rendered WITH the question — a list of
    ``MediaItem`` (or their dict form) each ``{"kind": "image"|"link", "url",
    "caption"?}``. An ``image`` url is an absolute ``https`` URL or a ``data:image/*``
    URI; a ``link`` url is an absolute ``http(s)`` URL; ``caption`` is the image alt
    text / link label. Bounded by a loose item count within a per-question total URI
    budget. It never becomes part of the answer — the human still answers via
    ``answer_format``. It renders in the Studio inbox AND, when a ``channel`` delivers
    the question, rides the delivery (as ``ChannelDelivery.media`` — the SAME stored
    items, a ``data:`` image already substituted to its served reference) so the
    channel shows it alongside the question text. Media is an ENHANCEMENT, not
    structure, so it rides no capability flag: a channel that renders only text simply
    ignores ``delivery.media`` and shows the question, never refusing the send.

    ``options`` is the SELECT answer set (required there) and, for a ``text`` question,
    an OPTIONAL list of SUGGESTED REPLIES: a tapped option submits its own text as the
    free-text answer (a text answer accepts any string, so a suggested reply constrains
    nothing). It is stored on the question and rides a ``channel`` delivery alike; it is
    forbidden on ``confirm``/``form``/``external``.

    ``mode`` selects the wait discipline. ``"sync"`` (the default) blocks and
    returns the typed answer as described above. ``"async"`` PARKS the caller: it
    persists (and optionally delivers) the question exactly as sync does but
    returns a ``SuspendedInteraction`` sentinel IMMEDIATELY instead of blocking,
    and a later answer OR expiry resumes work out of band by running the CURRENT
    driver's resume continuation. An async ask requires a resuming driver bound in
    the ``resume_continuation_tool`` context AND a bound execution identity to
    rebind that continuation as — both are raised loudly when absent, never
    silently degraded to a blocking wait. ``expiry_at`` is the async park deadline
    (when the parked question expires); it is mutually exclusive with a sync
    ``timeout`` (``check_ask_timing`` enforces it) and forbidden with ``mode="sync"``.
    """
    check_ask_timing(timeout=timeout, expiry_at=expiry_at)
    if mode != "async" and expiry_at is not None:
        raise ValueError("expiry_at is only valid with mode='async'")
    if mode == "async" and expiry_at is None:
        # An async park with no deadline is never expiry-indexed, so the reaper
        # could never fire its continuation and the idle TTL would drop it
        # silently — refuse it up front rather than persist an unresumable park.
        raise ValueError("async mode requires expiry_at")
    try:
        fmt = AnswerFormat(answer_format)
    except ValueError as exc:
        raise ValueError(f"unknown answer_format: {answer_format!r}") from exc

    is_external = fmt is AnswerFormat.EXTERNAL
    # ``options`` are the SELECT answer set (required there) and, for TEXT, an OPTIONAL set
    # of suggested replies a tap of which submits its OWN text as the free-text answer (text
    # accepts any string, so a suggested reply constrains nothing). Every other format
    # carries none — refuse loudly here, before any state is written, rather than silently
    # drop them. The SELECT-requires-options check stays in ``_build_payload``.
    if options is not None and fmt not in (AnswerFormat.SELECT, AnswerFormat.TEXT):
        raise ValueError(f"options are not valid with answer_format {fmt.value!r}")
    # ``audience`` (the addressed identity) is validated loud and up front — a
    # blank/whitespace value can never address a real identity — mirroring the
    # ``notify_user`` guard so both surfaces reject it identically before any
    # state is written.
    if audience is not None and (not isinstance(audience, str) or not audience.strip()):
        raise ValueError("audience must be a non-empty identity")
    # Write-side isolation clamp — before any state is written. A restricted caller
    # may address only its own slice: an unset audience is scoped to its own identity,
    # and any other identity is rejected loudly (cross-identity inject/exfil). An
    # unrestricted caller is unchanged.
    audience = clamp_write_audience(audience)
    # Channel validation, loud and up front — before any state is written
    # (mirrors the verifier guard, and mirrors the ``link`` guard's shape).
    # The resolved object is kept for the delivery below.
    channel_obj: Channel | None = None
    if channel is not None:
        channel_obj = _validate_channel(channel)
        if link is not None:
            # The channel owns the delivery surface for every format.
            raise ValueError("link is forbidden when a channel is set (the channel owns delivery)")
        if verifier is not None:
            # A channel's forward to the callback door is unsigned, so a bound
            # verifier would 401 every reply — the question could never be
            # answered. Reject loudly, never persist an unanswerable question.
            raise ValueError("verifier is forbidden when a channel is set (the channel forward is unsigned)")
        if fmt is AnswerFormat.FORM:
            if not getattr(channel_obj, "supports_form_delivery", False):
                # A form is delivered only over a channel that advertises the
                # ``supports_form_delivery`` capability; a channel without it can
                # never surface a multi-field form, so refuse loudly naming it.
                raise ValueError(f"channel {channel!r} does not deliver form questions")
            if schema is not None:
                # A channel form is answered on the server-rendered callback page,
                # so its schema must fall in the renderable subset — refuse anything
                # richer here, BEFORE any state is written (a missing schema is left
                # to ``_build_payload``'s own "requires a schema" guard). Normalize
                # once (pydantic model -> JSON schema) and carry the dict forward so
                # nothing re-normalizes it.
                schema = _normalize_schema(schema)
                validate_channel_form_schema(schema)
                # Channel-specific form limits (reserved names, per-medium Block
                # Kit / Flow caps, question-text caps) the generic subset does not
                # know: the channel's OPTIONAL ``validate_form_schema`` hook enforces
                # them at the chokepoint — before any state is written — raising
                # ``ValueError`` on a violation, so a question or schema the channel
                # could never render is refused up front rather than persisted and
                # failed at delivery.
                validate_form_schema = getattr(channel_obj, "validate_form_schema", None)
                if validate_form_schema is not None:
                    validate_form_schema(schema, question)
        if recipient is not None and (not isinstance(recipient, str) or not recipient.strip()):
            # Rejected up-front as a clean ValueError — never a post-persist
            # pydantic error from the delivery frame's own recipient validator.
            raise ValueError("recipient must be a non-empty address")
    elif recipient is not None:
        # An address is meaningless without a channel to send on; the named
        # channel is what carries (and allowlist-validates) the recipient.
        raise ValueError("recipient requires a channel (an address is meaningless without one)")
    # Combo validation, loud and up front. For external, the schema is normalized
    # here too so a bad schema fails BEFORE the link builder does external work.
    if is_external:
        if link is None and channel is None:
            raise ValueError("answer_format 'external' requires a link (or a channel)")
        if schema is not None:
            _normalize_schema(schema)
        if verifier is not None:
            _validate_verifier(verifier)
    else:
        if link is not None:
            raise ValueError("link is only valid with answer_format 'external'")
        # A verifier authenticates the external server-to-server callback; on a
        # human-answerable format it would emit ``server_verified`` and make the
        # UI render a non-actionable card no human can ever answer. Reject it
        # loudly, mirroring the ``link`` guard — a hard guard, not a soft ignore.
        if verifier is not None:
            raise ValueError("verifier is only valid with answer_format 'external'")

    settings = interactions_settings()
    # OFF gate — a loud, named raise before any state is written (surfaces as a
    # ToolError, matching this helper's existing loud contract): an unconfigured
    # interactions store cannot hold the question, so ``ask_user`` fails naming the
    # env var that turns the feature on rather than reaching for an absent Redis.
    require(settings.redis.redis_url, "the interactions store", "INTERACTIONS_REDIS_URL", "TAI_DEFAULT_REDIS_URL")
    # Async resolves its resume continuation up front, before any state is
    # written: an async ask with no bound driver or no execution identity to
    # rebind it as is a caller error that must fail loudly, never persist a
    # question no answer/expiry could ever resume.
    continuation_tool: str | None = None
    continuation_identity: str | None = None
    continuation_fingerprint: str | None = None
    # The conversation thread this park binds to (None for a sync ask or an unbound run):
    # captured so a later thread delete can cascade-cancel the park via its reverse index.
    park_thread_id: str | None = None
    if mode == "async":
        continuation_tool = get_resume_continuation_tool()
        if continuation_tool is None:
            raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
        # Function-local: a module-level edge from here into ``authz`` closes an
        # import cycle (authz → access_control.backend) that crashes any process
        # importing ``access_control`` first.
        from tai42_skeleton.authz.execution_identity import get_execution_identity

        identity = get_execution_identity()
        if identity is None or identity.user_id is None:
            raise RuntimeError("async ask requires a bound execution identity to rebind the continuation as")
        continuation_identity = identity.user_id
        # Stashed alongside the identity so the answer/expiry path rebinds the
        # continuation under the SAME fire authority the ask ran under; gate-off
        # fires carry no fingerprint, recorded as "" (the bind ignores it there).
        continuation_fingerprint = identity.execution_key_fingerprint or ""
        # The bound conversation thread (from the turn layer's park-completion / bridge
        # context), so this park joins its thread's reverse index and a thread delete can
        # cancel it. ``None`` outside a bound conversation turn — indexed then only by its
        # own expiry, exactly as today.
        park_thread_id = _bound_park_thread_id()

    budget = settings.answer_timeout_seconds if timeout is None else timeout
    if budget <= 0:
        # Redis BLPOP treats 0 as "block forever" — the opposite of no-wait —
        # so a non-positive budget can never mean anything sane here.
        raise ValueError(f"timeout must be positive, got {budget!r}")
    created_at = datetime.now(UTC)
    # The STORED deadline: a sync question expires at its answer budget; an async
    # park expires at its ``expiry_at`` (required above), NOT the sync budget — no
    # caller blocks on it, and the expiry reaper is what resumes work when it passes.
    if mode == "async":
        assert expiry_at is not None  # async requires expiry_at (guarded above)
        timeout_at = expiry_at
    else:
        timeout_at = created_at + timedelta(seconds=budget)
    # ONE monotonic deadline for the SYNCHRONOUS phase — the delivery attempts,
    # their backoff sleeps, and (sync only) the answer wait — anchored with
    # ``created_at`` at the answer budget. Delivery time shrinks a sync caller's
    # answer wait, and a sync caller can never block past the budget. An async park
    # does not wait, so its STORED ``timeout_at``/ticket TTL run to the park deadline
    # (``expiry_at``), which may exceed this budget; only the delivery phase is bound
    # by it, never the park's own lifetime.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget

    interaction_id = str(uuid.uuid4())
    group = group_id or str(uuid.uuid4())
    store = InteractionStore(settings.key_prefix)
    reply_to = store.reply_key(interaction_id)

    # An async park's keys must outlive its ``expiry_at`` by enough for the reaper
    # to fire — at least a couple of its passes — or the state hash would expire
    # before the reaper reads it, stranding the continuation. Tie the margin to the
    # reaper interval so it holds under any configured cadence. This is the ONE
    # source for the async-park margin: the state ``_key_ttl`` (via ``add``'s
    # ``expiry_ttl_margin_seconds``) AND the callback ticket TTL below both add this
    # exact value, so ``expiry_at + margin`` is the shared answerable window for the
    # authenticated ``/answer`` door and the channel-delivery callback door alike —
    # the reaper claims the park at ``expiry_at``, so both reject a late answer past
    # it. (The state hash TTL may floor to ``idle_ttl`` as a storage backstop when
    # the park is short; that is not an extended answerable window — the ticket and
    # the reaper still bound answering to ``expiry_at + margin``.)
    park_ttl_margin_seconds = 2 * math.ceil(settings.expiry_reaper_interval_seconds)

    ticket: str | None = None
    ticket_ttl: int | None = None
    callback_url: str | None = None
    if is_external or channel is not None:
        # A channel bridges the human's reply back through the public callback
        # door, so a set channel forces the ticket + callback-URL mint for EVERY
        # answer format. The settings validator guarantees a set public_base_url
        # is https:// (or localhost http://); only absence is checked here.
        if settings.public_base_url is None:
            raise RuntimeError(
                "external answer_format (and channel delivery) requires INTERACTIONS_PUBLIC_BASE_URL to be set"
            )
        ticket = secrets.token_urlsafe(32)
        # TTL = seconds until the STORED deadline, ceiled so the ticket always
        # outlives it (floor 1s): a sync question's answer budget, or an async
        # park's ``expiry_at`` horizon PLUS the same reaper margin the state
        # ``_key_ttl`` uses — so the callback door and the authenticated ``/answer``
        # door stay answerable through the identical park window, never one door
        # rejecting a late answer the other still accepts. Never deleted on claim.
        ticket_ttl = max(1, math.ceil((timeout_at - created_at).total_seconds()))
        if mode == "async":
            ticket_ttl += park_ttl_margin_seconds
        callback_url = f"{settings.public_base_url.rstrip('/')}/api/interactions/callback/{ticket}"

    if is_external:
        assert callback_url is not None  # is_external forces the mint above
        if channel is not None:
            # Channel-delivered external ask: the channel presents the tappable
            # URL and that URL IS the callback door (GET confirm page / POST
            # answer sink) — no link builder runs.
            final_url = callback_url
        else:
            # ``link`` is non-None here (validated above); resolve BEFORE any
            # persist so a failed builder leaves zero state.
            final_url = await _resolve_link(link, callback_url)  # type: ignore[arg-type]
        format_payload = _build_payload(fmt, options, schema, url=final_url, verifier=verifier)
    else:
        format_payload = _build_payload(fmt, options, schema)

    async with client_ctx(RedisClient, settings.redis) as r:
        # A data:image is decoded once and stored BY REFERENCE before the request is
        # built, so the durable record carries a served reference (``MEDIA_ROUTE_PREFIX{id}``)
        # the inbox renders, never the inline bytes. The media keys start at ``idle_ttl`` and
        # ``add`` extends them to the group horizon (an async park's own longer horizon
        # included). https/link items pass through unchanged. ``substitute_media`` coerces
        # every item through ``MediaItem`` — the media validation that raises before any
        # store. dicts and MediaItem inputs are both accepted.
        #
        # This SAME stored list rides both the durable record (the inbox) AND, on a channel
        # ask, the ``ChannelDelivery.media`` forwarded to the plugin. A channel vendor fetches
        # media from its own servers, so a channel ask must carry an ABSOLUTE served url — a
        # relative ``MEDIA_ROUTE_PREFIX{id}`` is unfetchable off-origin. So pass ``base_url``
        # when a channel is set (``public_base_url`` is already hard-required above whenever a
        # channel is set, so it is non-None here — a data: ask with no public base url has
        # already failed loudly, matching the notify path's data:-image refusal); an
        # inbox-only ask keeps the relative same-origin url the inbox renders.
        stored_media = (
            await substitute_media(
                store,
                r,
                media,
                settings.idle_ttl_seconds,
                base_url=settings.public_base_url if channel is not None else None,
            )
            if media is not None
            else None
        )
        request = InteractionRequest(
            interaction_id=interaction_id,
            group_id=group,
            question=question,
            answer_format=fmt,
            format_payload=format_payload,
            reply_to=reply_to,
            created_at=created_at,
            timeout_at=timeout_at,
            sensitive=sensitive,
            channel=channel,
            recipient=recipient,
            audience=audience,
            # Origin of the raising run (a background tool-run id), read from the
            # contextvar the run binds; None outside a bound tool run.
            origin=get_interaction_origin(),
            media=stored_media,
            # Async park: the sentinel-returning discipline plus the generic
            # continuation resolved above (both None for a sync ask). The model's own
            # validator enforces continuation-iff-async.
            mode=mode,
            continuation_tool=continuation_tool,
            continuation_identity=continuation_identity,
            expiry_at=expiry_at,
        )
        # Concurrency guard (all formats). ``reserve_open_slot`` prunes stale open
        # members, refuses at the cap, and reserves this question's open-index
        # member in ONE atomic step — so a concurrent burst admits exactly
        # ``max_concurrent`` callers and refuses the rest, with no check-then-act
        # overshoot. A reserved slot means ``add`` must skip re-adding the member.
        if settings.max_concurrent is not None:
            reserved = await store.reserve_open_slot(r, request, settings.max_concurrent)
            if not reserved:
                raise InteractionLimitError(
                    f"ask_user refused: already at the max_concurrent limit ({settings.max_concurrent})"
                )
            await store.add(
                r,
                request,
                settings.idle_ttl_seconds,
                ticket=ticket,
                ticket_ttl=ticket_ttl,
                open_member_reserved=True,
                continuation_fingerprint=continuation_fingerprint,
                expiry_ttl_margin_seconds=park_ttl_margin_seconds,
                thread_id=park_thread_id,
            )
        else:
            await store.add(
                r,
                request,
                settings.idle_ttl_seconds,
                ticket=ticket,
                ticket_ttl=ticket_ttl,
                continuation_fingerprint=continuation_fingerprint,
                expiry_ttl_margin_seconds=park_ttl_margin_seconds,
                thread_id=park_thread_id,
            )

    # Deliver through the channel AFTER the question is persisted (the callback
    # ticket must be claimable before any human can act on it) and BEFORE the
    # blocking wait. Each ``deliver`` is ONE send attempt; a failure the plugin
    # typed as ``retryable`` (a medium 5xx, a rate limit, a transport fault, a
    # hung send) is re-attempted up to ``delivery_max_attempts`` with exponential
    # backoff, and everything else — an unknown fault included — fails on the
    # first try rather than being blind-retried. The whole phase, attempts and
    # backoff sleeps together, shares ONE monotonic deadline with the answer wait
    # that follows: the timeout budget bounds them TOGETHER, so delivery time
    # shrinks the answer wait and the caller can never be blocked past the budget
    # with the question persisted (a delivery phase that eats the whole budget
    # leaves no wait and times out).
    # A FINAL failure normally means the human never received the question, so
    # the persisted state must not linger open/claimable: prune, then re-raise
    # loudly. The ONE exception is when the prune finds nothing pending —
    # ``"answered"`` (a fast reply beat the failure) or ``"gone"`` (the record
    # expired/was pruned elsewhere): a recorded answer is never discarded, so fall
    # through to the blocking wait, which returns it immediately or times out.
    if channel is not None:
        assert channel_obj is not None  # resolved with the up-front validation
        assert callback_url is not None  # a set channel forces the mint above
        # Built once and reused across attempts — the frame is frozen.
        delivery_frame = ChannelDelivery(
            interaction_id=interaction_id,
            recipient=recipient,
            question=question,
            answer_format=fmt.value,
            options=options,
            # For a form the normalized schema rides the delivery; ``_build_payload``
            # already required and normalized it above (before any persist).
            schema=(format_payload or {}).get("schema") if fmt is AnswerFormat.FORM else None,
            # The question's display media rides the delivery too — the SAME stored items,
            # here with any data: image substituted to an ABSOLUTE served reference (the
            # channel branch above passed ``base_url``), so a vendor can fetch it off-origin;
            # a channel that renders media shows it alongside the question and one that
            # renders only text simply ignores it. None when the ask carried no media.
            media=stored_media,
            callback_url=callback_url,
            timeout_at=timeout_at,
        )
        attempt = 0
        retry_in: float | None = None
        while True:
            attempt += 1
            try:
                if retry_in is not None:
                    await asyncio.sleep(retry_in)
                # Each attempt is bounded by what is left of the budget: a
                # plugin that consumes it is hung, and an unbounded await here
                # would block the caller forever with the question persisted —
                # so a timeout is a typed, retryable delivery failure.
                attempt_timeout = deadline - loop.time()
                try:
                    await asyncio.wait_for(channel_obj.deliver(delivery_frame), timeout=attempt_timeout)
                except TimeoutError as exc:
                    raise ChannelDeliveryError(
                        f"channel {channel!r} delivery timed out after {attempt_timeout:.1f}s "
                        f"of the ask's {budget}s budget (interaction {interaction_id})",
                        retryable=True,
                    ) from exc
            except BaseException as exc:
                retry_in = _retry_delay(exc, attempt, deadline - loop.time(), settings)
                if retry_in is not None:
                    # Intermediate failure: the question stays open for the next
                    # attempt — never pruned here, never silent.
                    logger.warning(
                        "channel %r delivery attempt %d/%d failed for interaction %s; retrying in %ss",
                        channel,
                        attempt,
                        settings.delivery_max_attempts,
                        interaction_id,
                        retry_in,
                        exc_info=exc,
                    )
                    continue
                result = await _prune(settings, store, interaction_id, group)
                if result == "pruned" or not isinstance(exc, Exception):
                    # Pruned → nothing was answered; propagate the failure loudly.
                    # A non-Exception (asyncio.CancelledError mid-send or
                    # mid-backoff, SystemExit) ALWAYS propagates — cancellation is
                    # never retried and never swallowed, even when an answer was
                    # recorded.
                    if isinstance(exc, asyncio.CancelledError):
                        mark_parked_question(exc, interaction_id, question, sensitive)
                    elif result == "pruned" and isinstance(exc, Exception):
                        # Terminal delivery abandonment: the send failed for good and the
                        # question was pruned (nothing answered). State the fact as a
                        # best-effort platform event that RIDES ALONGSIDE the raise below —
                        # a deployment wires a hook on the topic to decide what an operator
                        # sees. Never a cancellation (handled above), never the
                        # answered/gone fall-through (an answer landed, delivery not
                        # abandoned), and never a replacement for the error path.
                        await _emit_delivery_failed(
                            channel=channel,
                            interaction_id=interaction_id,
                            recipient=recipient,
                            error=str(exc),
                        )
                    raise
                if result == "answered":
                    logger.warning(
                        "channel %r delivery failed for interaction %s after the answer"
                        " was already recorded; falling through to the answer wait",
                        channel,
                        interaction_id,
                        exc_info=exc,
                    )
                else:
                    logger.warning(
                        "channel %r delivery failed for interaction %s but the question"
                        " record was already gone; falling through to the answer wait",
                        channel,
                        interaction_id,
                        exc_info=exc,
                    )
            break

    # Async park: the question is persisted (and, when a channel was given,
    # delivered) exactly as sync — but the caller is NOT blocked. Return the
    # sentinel now; a later answer (either door) or the expiry reaper resumes work
    # by running the stored generic continuation as the stored identity.
    if mode == "async":
        return SuspendedInteraction(interaction_id=interaction_id, expiry_at=expiry_at)

    # The answer wait gets what is LEFT of the budget after delivery — the same
    # deadline the delivery attempts ran against — so the whole ask stays inside
    # the budget. A delivery phase that consumed it all leaves nothing to wait on;
    # Redis BLPOP reads its timeout at 1ms resolution and a 0/negative timeout as
    # "block forever", so a sub-millisecond remainder degrades to 0 = block-forever
    # too — anything below 1ms skips the wait entirely and takes the timeout path
    # directly.
    remaining = deadline - loop.time()
    if remaining < 0.001:
        response = None
    else:
        # Block for the answer on a dedicated connection: a human-scale wait holds
        # its connection for the remaining wait (up to the budget), so pinning one
        # from the shared pool would starve other concurrent ask_user calls once the
        # pool is drained.
        try:
            # Strip the socket read timeout on this connection only: the BLPOP blocks
            # legitimately for the remaining wait, so a blanket 5s read timeout would
            # kill it. The store wraps the BLPOP in an outer wait_for (the passed
            # timeout + grace) instead, so a black-holed redis still fails loudly.
            reply_redis = settings.redis.model_copy(update={"socket_timeout": None})
            async with client_ctx(RedisClient, reply_redis, fresh=True) as reply_conn:
                response = await store.wait_for_reply(reply_conn, reply_to, remaining, settings.blocking_grace_seconds)
        except asyncio.CancelledError as exc:
            # Prune on cancel so an abandoned question does not inflate the group
            # count / open index. The status gate makes the cancelled-after-answer
            # race a no-op. A cleanup failure propagates (chained on the
            # CancelledError context), never swallowed.
            mark_parked_question(exc, interaction_id, question, sensitive)
            await _prune(settings, store, interaction_id, group)
            raise
    if response is None:
        # Timeout: prune first, else the abandoned question inflates the group
        # count until the idle TTL and stays claimable by a late callback. The
        # prune result names which of the three end states the question reached,
        # and each message asserts only what that state guarantees: ``"pruned"`` an
        # open question was removed (genuinely no answer); ``"answered"`` an answer
        # was recorded after the budget and this caller never got it; ``"gone"``
        # the record had already vanished (expired, or pruned elsewhere) with no
        # answer returned.
        result = await _prune(settings, store, interaction_id, group)
        if result == "pruned":
            raise InteractionTimeoutError(
                f"ask_user timed out after {budget}s with no answer (interaction {interaction_id})"
            )
        if result == "answered":
            raise InteractionTimeoutError(
                f"ask_user timed out after {budget}s; an answer was recorded after the budget "
                f"and was not returned (interaction {interaction_id})"
            )
        raise InteractionTimeoutError(
            f"ask_user timed out after {budget}s; the question record was already gone "
            f"(expired or pruned elsewhere) and no answer was returned (interaction {interaction_id})"
        )
    # A sensitive answer is handed back wrapped so it cannot leak through a repr,
    # a log line, or a JSON dump — the caller reveals it deliberately.
    if sensitive:
        return SecretValue(response.answer)
    return response.answer
