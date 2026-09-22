"""The author-facing ``ask`` surface — the ``Ask`` contract impl.

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

# ``secrets`` is the callback-ticket token source ``ask.timing`` mints with; it is
# re-exported here so this module and the mint reference one shared module object.
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from tai42_contract.interactions import (
    AnswerMismatchPolicy,
    FormData,
    FormPage,
    MediaItem,
    SuspendedInteraction,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import require

from tai42_skeleton.interactions.ask import delivery, park, persist, timing, validate, wait
from tai42_skeleton.interactions.ask import payload as payload_shaping
from tai42_skeleton.interactions.ask.errors import InteractionLimitError, InteractionTimeoutError
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore

__all__ = [
    "DELIVERY_FAILED_EVENT_TOPIC",
    "InteractionLimitError",
    "InteractionTimeoutError",
    "ask",
    "cancel_parks_for_person",
    "cancel_parks_for_thread",
    "client_ctx",
    "rekey_parks_for_merge",
    "secrets",
]

# The platform-event topic emitted when a channel delivery of a question is
# TERMINALLY abandoned (retries exhausted / non-retryable / no budget left). Core
# states the fact; a deployment wires a hook (topic -> a tool such as notify_user, a
# ticket) in config to decide what an operator sees. It RIDES ALONGSIDE the unchanged
# raise that propagates the failure — it never replaces the error path.
DELIVERY_FAILED_EVENT_TOPIC = "interactions_delivery_failed"


async def cancel_parks_for_thread(thread_id: str, *, reason: str = "thread_deleted") -> list[str]:
    """Whole-chain-kill every async ``ask`` park addressed to ``thread_id``.

    The entry point a conversation thread/route delete calls so a parked question the deletion would
    orphan is torn down instead of lingering muted until its expiry deadline. Reaches every park two
    ways — the thread reverse index (a thread-bound background park) UNION the subject index for
    ``(kind="thread", key=thread_id)`` (a conversation park on any scope) — and routes each through
    the ONE whole-chain kill seam (:func:`kill_park`): the driver teardown fires and the run's single
    FAILED is delivered, never a bare prune. Runs on its own connection. A no-op when the
    interactions store is unconfigured. Idempotent — safe to re-run under a delete's retry. Returns
    the interaction ids reached.
    """
    from tai42_skeleton.interactions.kill import kill_members

    settings = interactions_settings()
    if not settings.redis.redis_url:
        # Interactions off: no park could ever have been persisted, so there is nothing
        # to cancel (and no Redis to reach for). The delete op proceeds unaffected.
        return []
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as conn:
        thread_members = await store.thread_park_members(conn, thread_id)
        members = [*thread_members, *await store.subject_members(conn, "thread", thread_id)]
        reached = await kill_members(conn, store, members, reason=reason)
        # Reconcile the reverse index: the kill prune already dropped a pruned member, this drops an
        # orphan whose state had vanished, and a concurrently-added park keeps its member.
        await store.reconcile_thread_park_members(conn, thread_id, thread_members)
    return reached


async def cancel_parks_for_person(person_id: str, *, reason: str = "person_erased") -> list[str]:
    """Whole-chain-kill every park a person forget reaches — its aggregated thread AND its person subject.

    A person's parks are addressed both under the aggregated ``bridge:@person:{id}`` thread and under
    ``(kind="person", key=person_id)``, so the erase kills across both to reach a park on any scope.
    Runs on one connection; idempotent under retry. Returns the interaction ids reached.
    """
    from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
    from tai42_skeleton.interactions.kill import kill_members

    settings = interactions_settings()
    if not settings.redis.redis_url:
        return []
    store = InteractionStore(settings.key_prefix)
    thread_id = f"{PERSON_THREAD_PREFIX}{person_id}"
    async with client_ctx(RedisClient, settings.redis) as conn:
        thread_members = await store.thread_park_members(conn, thread_id)
        members = [
            *thread_members,
            *await store.subject_members(conn, "thread", thread_id),
            *await store.subject_members(conn, "person", person_id),
        ]
        reached = await kill_members(conn, store, members, reason=reason)
        await store.reconcile_thread_park_members(conn, thread_id, thread_members)
    return reached


async def rekey_parks_for_merge(absorbed_id: str, survivor_id: str) -> None:
    """Re-key every park/outcome of an absorbed person onto the survivor — the person merge.

    Moves the absorbed person's subject index membership, stored subject descriptors and thread
    index across for BOTH re-keyed subject keys: the person id ``(kind="person")`` and the aggregated
    person-thread key ``(kind="thread")``. A no-op when the interactions store is unconfigured or the
    two ids are equal. Idempotent under retry.
    """
    from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX

    if absorbed_id == survivor_id:
        return
    settings = interactions_settings()
    if not settings.redis.redis_url:
        return
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as conn:
        await store.rekey_subject(conn, kind="person", old_key=absorbed_id, new_key=survivor_id)
        await store.rekey_subject(
            conn,
            kind="thread",
            old_key=f"{PERSON_THREAD_PREFIX}{absorbed_id}",
            new_key=f"{PERSON_THREAD_PREFIX}{survivor_id}",
        )


async def ask(
    question: str,
    *,
    answer_format: str = "text",
    options: list[str] | None = None,
    schema: type[BaseModel] | dict[str, Any] | None = None,
    data: FormData | dict[str, Any] | None = None,
    pages: list[FormPage] | list[dict[str, Any]] | None = None,
    group_id: str | None = None,
    timeout: float | None = None,
    link: str | Callable[[str], Awaitable[str]] | None = None,
    verifier: dict[str, Any] | None = None,
    channel: str | None = None,
    recipient: str | None = None,
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY,
    mismatch_notice: str | None = None,
    sensitive: bool = False,
    audience: str | None = None,
    media: list[MediaItem | dict[str, Any]] | None = None,
    mode: Literal["sync", "async"] = "sync",
    expiry_at: datetime | None = None,
    to: Literal["user", "caller"] = "user",
    payload: dict[str, Any] | None = None,
    on_expiry: Literal["kill", "resume"] = "kill",
) -> Any:
    """Ask a human ``question`` and return their answer.

    In ``mode="sync"`` block until the answer returns; in ``mode="async"`` park the caller and
    return a ``SuspendedInteraction`` immediately.

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

    ``on_mismatch`` is the per-ask digression policy the shared inbound-answer
    ladder reads when the answer door REJECTS a participant reply on a LIVE
    channel-delivered ask (a 400 — the reply did not fit the format).
    ``AnswerMismatchPolicy.RETRY`` (the default, today's behavior) keeps the ask
    parked and tells the participant what is expected so they answer again in place;
    ``AnswerMismatchPolicy.BRIDGE`` treats an unmatched reply as a DIGRESSION —
    keep the ask parked with NO participant notice and hand the reply to the
    conversation as a fresh routed turn, so the ask ends only by a real answer or
    its timeout. It rides both the durable ``InteractionRequest`` (attribution) and
    the ``ChannelDelivery`` the channel copies onto the ``Correlation`` it parks
    (the ladder's authoritative read). It takes effect only on a channel-delivered
    ask; an inbox-only ask records it but never reaches the ladder.

    ``mismatch_notice`` is an OPTIONAL custom participant-facing rejection notice used
    ONLY under the ``RETRY`` policy: when set it REPLACES the platform's built-in
    retry notice (a literal ``{reason}`` token is filled with the door's reason by
    a plain substitution; a notice without it is sent verbatim). It is IGNORED
    under ``BRIDGE`` (a digression never notifies) and by a channel that owns its
    correction surface. It rides the same two frames as ``on_mismatch``. ``None``
    (the default) uses the built-in notice; a set value is a non-blank string
    within the participant-reply cap (the models enforce it).

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

    ``data`` and ``pages`` enrich a ``form`` ask for ONE send (forbidden on every other
    format, refused loudly). ``data`` (a ``FormData`` or its dict) prefills ``values``
    into the form's controls and supplies per-send ``options`` — a choice list that
    REPLACES a property's schema ``enum`` for this send only, so a variant needs no
    re-published form. ``pages`` (a list of ``FormPage`` or their dicts) splits the form
    into ordered steps, each naming the top-level properties it collects; every property
    appears exactly once and absent ``pages`` means one page. Both are validated against
    the form's schema when the ``InteractionRequest`` is built (before any state is
    written) and, on a ``channel`` ask, ride the delivery so the channel renders the
    prefill, the per-send choices and the steps; the answer is the union of all fields.

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

    ``to`` addresses the ask. ``"user"`` (the default) is a human answered through the
    inbox/callback/channel surfaces. ``"caller"`` addresses another RUN: the ask carries NO
    out-of-band delivery — no callback ticket, no channel send, no notification — it parks the
    asking run and its answer is handed back by the run that resolves it. A ``"caller"`` ask is
    always ``mode="async"`` (a sync one is refused) and requires an ambient state context.

    ``payload`` is a structured value a ``"caller"`` ask hands the resolving run in place of (or
    beside) ``question``; when given, ``question`` may be empty. Forbidden on a ``"user"`` ask.

    ``on_expiry`` selects what the expiry reaper does when a parked ask's deadline lapses
    unanswered: ``"kill"`` (the default) tears the whole run chain down; ``"resume"`` resumes the
    continuation with the expiry marker. Read only for an async park.
    """
    validation = validate.validate_ask_arguments(
        question,
        answer_format=answer_format,
        options=options,
        schema=schema,
        data=data,
        pages=pages,
        timeout=timeout,
        link=link,
        verifier=verifier,
        channel=channel,
        recipient=recipient,
        audience=audience,
        mode=mode,
        expiry_at=expiry_at,
        to=to,
        payload=payload,
    )
    fmt = validation.fmt
    schema = validation.schema
    audience = validation.audience

    settings = interactions_settings()
    # OFF gate — a loud, named raise before any state is written: an unconfigured
    # interactions store cannot hold the question, so ``ask`` fails naming the
    # env var that turns the feature on rather than reaching for an absent Redis.
    require(settings.redis.redis_url, "the interactions store", "INTERACTIONS_REDIS_URL", "TAI_DEFAULT_REDIS_URL")

    # Async resolves its resume continuation up front, before any state is written: an
    # async ask with no bound driver or no identity to rebind it as is a caller error
    # that must fail loudly. A sync ask carries an empty (all-None) binding.
    park_binding = park.resolve_async_continuation(to) if mode == "async" else park.AsyncParkBinding()

    window = timing.resolve_deadline(mode, timeout, expiry_at, settings)

    interaction_id = str(uuid.uuid4())
    group = group_id or str(uuid.uuid4())
    store = InteractionStore(settings.key_prefix)
    reply_to = store.reply_key(interaction_id)

    # A ``to="caller"`` ask addresses another RUN, never a human: it performs NO send of any
    # kind (no callback ticket, no external link, no channel delivery, no re-park notice). Its
    # question fields are shape-validated above but not acted on; its answer is handed back by
    # the run that resolves it.
    caller_addressed = to == "caller"

    # A set channel or the external format bridges the reply back through the public
    # callback door, so it forces the ticket + callback-URL mint for EVERY answer format —
    # never for a caller-addressed ask, which is not delivered.
    callback = (
        None
        if caller_addressed
        else timing.mint_callback_ticket(settings, window, mode, force=validation.is_external or channel is not None)
    )

    if validation.is_external and not caller_addressed:
        if callback is None:
            raise AssertionError
        if channel is not None:
            # Channel-delivered external ask: the channel presents the tappable URL and
            # that URL IS the callback door — no link builder runs.
            final_url = callback.callback_url
        else:
            # ``link`` is non-None here (validated above); resolve BEFORE any persist so
            # a failed builder leaves zero state.
            final_url = await payload_shaping.resolve_link(link, callback.callback_url)  # type: ignore[arg-type]
        format_payload = payload_shaping.build_payload(fmt, options, schema, url=final_url, verifier=verifier)
    else:
        format_payload = payload_shaping.build_payload(fmt, options, schema, data=data, pages=pages)

    stored_media = await persist.persist_question(
        store,
        settings,
        window,
        callback,
        park_binding,
        interaction_id=interaction_id,
        group=group,
        question=question,
        fmt=fmt,
        format_payload=format_payload,
        on_mismatch=on_mismatch,
        mismatch_notice=mismatch_notice,
        reply_to=reply_to,
        sensitive=sensitive,
        channel=channel,
        recipient=recipient,
        audience=audience,
        media=media,
        mode=mode,
        expiry_at=expiry_at,
        to=to,
        payload=payload,
        on_expiry=on_expiry,
    )

    # Deliver through the channel AFTER the question is persisted (the callback ticket
    # must be claimable before any human can act on it) and BEFORE the blocking wait.
    # A caller-addressed ask is never delivered — it addresses a run, not a human.
    if channel is not None and not caller_addressed:
        if validation.channel_obj is None:
            raise AssertionError
        if callback is None:
            raise AssertionError
        delivery_frame = delivery.build_delivery_frame(
            interaction_id=interaction_id,
            recipient=recipient,
            question=question,
            fmt=fmt,
            options=options,
            format_payload=format_payload,
            stored_media=stored_media,
            on_mismatch=on_mismatch,
            mismatch_notice=mismatch_notice,
            callback_url=callback.callback_url,
            timeout_at=window.timeout_at,
        )
        await delivery.deliver_with_retry(
            validation.channel_obj,
            delivery_frame,
            settings,
            store,
            window,
            channel=channel,
            recipient=recipient,
            interaction_id=interaction_id,
            group=group,
            question=question,
            sensitive=sensitive,
        )

    # Async park: the question is persisted (and, when a channel was given, delivered)
    # exactly as sync — but the caller is NOT blocked. Return the sentinel now; a later
    # answer (either door) or the expiry reaper resumes work by running the stored
    # generic continuation as the stored identity.
    if mode == "async":
        # A CHAINED caller waiting on this run inherited ITS horizon from the run's
        # previous ask, so tell it this park's deadline. Fired only under a chained
        # binding and only once the park is persisted — never for a caller-addressed ask,
        # which performs no send of any kind.
        if not caller_addressed:
            await park.notify_repark(expiry_at, interaction_id=interaction_id)
        # The sentinel names the resume continuation this park was stamped with, so a
        # caller that would adopt the park as its OWN suspended state can check it owns
        # it instead of parking behind a resume fired elsewhere. ``caller_interaction_ids``
        # names this single ask when it is caller-addressed (empty for a user ask), so a
        # driver surfacing a whole step can tell the caller asks apart.
        return SuspendedInteraction(
            interaction_id=interaction_id,
            expiry_at=expiry_at,
            resume_owner=park_binding.continuation_tool,
            interaction_ids=[interaction_id],
            caller_interaction_ids=[interaction_id] if caller_addressed else [],
        )

    return await wait.await_answer(
        store,
        settings,
        window,
        interaction_id=interaction_id,
        group=group,
        reply_to=reply_to,
        question=question,
        sensitive=sensitive,
    )
