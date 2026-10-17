"""Pending-question correlation store (plugin-owned Redis).

A WhatsApp reply carries no thread, so the only correlation key is the pair
(<phone_number_id>, <wa_id>). AT MOST ONE pending question per pair — an atomic
``SET NX`` reservation; a second concurrent question is rejected loudly with
``PendingQuestionExistsError``. The value holds the ``callback_url`` and deadline;
the key TTL is the remaining answer budget, so an expired question cannot capture
a later reply.

The record is read NON-DESTRUCTIVELY: ``peek_pending`` returns the rich record
without claiming it, and the shared ladder (through the ``CorrelationStore``
port's ``get_correlation``/``release_correlation``) decides when the reservation
is released — a kept rejection leaves the record untouched, and only a terminal
outcome deletes it. ``bump_rejections`` increments the re-ask counter in place on
the still-held record. Exclusivity lives entirely on the ``SET NX`` reserve. When the
platform takes a delivered question down, ``release_pending_for`` drops the pair's
record atomically ONLY while it is still held for that question's interaction id, so a
newer ask's hold on the same pair is never released.

A select ask also carries its ``options`` and ``interaction_id`` so an inbound
interactive tap (whose id is ``{interaction_id}:{index}``) maps back to the exact
option text under the exact ask it was sent for.

A published-Flow cache maps ``(waba_id, flow_identity)`` to a Meta flow id with NO
TTL — a published Flow persists on Meta, so the id is reused for every form send
sharing that Flow; a cache miss resolves the Flow on the WABA (found by name, else
created and published) and stores it. The miss runs under a per-``(waba_id,
flow_identity)`` redis lock so concurrent first sends resolve one Flow.
Beside it, a schema sidecar maps ``(waba_id, schema_hash)`` to the answer
schema itself PLUS the reverse of its schema-key → completion-payload-key map (each
completion-payload key back to its schema key), its pages and option fields, for ask-less
form notifications whose reply carries only the hash (inside its flow token) and no
reservation — the map decodes the reply's payload-keyed answers back to schema keys before
coercion, and the data endpoint checks an endpoint-driven notify form's steps with the rest.
Each completion-payload key is the field's human-readable label (its schema ``title``, else
the property key, disambiguated ``_2``, ``_3`` … on a collision). An endpoint-driven form
ask keeps an :class:`EndpointForm` sidecar under its interaction id for the data endpoint.

A handled-``wamid`` set is the replay guard (a redelivered webhook repeats the id).

A KNOWN-CONTACT marker records that a ``(phone_number_id, wa_id)`` pair sent an
inbound message within a rolling window (TTL refreshed per inbound); the
template-send recipient policy reads it to admit a pair the operator has not
allowlisted but who opened the conversation window.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, cast

from redis.exceptions import LockError, LockNotOwnedError
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, Correlation
from tai42_contract.interactions.models import AnswerMismatchPolicy, Wording
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_whatsapp.settings import (
    WhatsAppRedisSettings,
    whatsapp_redis_settings,
    whatsapp_settings,
)

logger = logging.getLogger(__name__)


class PendingQuestionExistsError(ChannelDeliveryError):
    """A question is already pending for this pair (one at a time)."""


@dataclass(frozen=True)
class PendingQuestion:
    """The pending question reserved for one ``(phone_number_id, wa_id)`` pair."""

    callback_url: str
    timeout_at: datetime
    # A select ask carries its option list and interaction id so an interactive
    # tap (id ``{interaction_id}:{index}``) resolves to the exact option text
    # under the exact ask; a text ask leaves both None.
    options: list[str] | None = None
    interaction_id: str | None = None
    # A form ask carries its answer schema so an inbound Flow response (nfm_reply)
    # is coerced to the schema's types before it is forwarded; non-form asks None.
    schema: dict[str, Any] | None = None
    # A form ask carries its question text so a door-rejected answer can be re-asked
    # with a fresh Flow whose body repeats the question; non-form asks None.
    question: str | None = None
    # A form ask also carries the per-send inputs the original send built its Flow from
    # — the step layout, the prefilled values, and the per-send option lists (the plain
    # JSON the send resolved) — so a door-rejection re-send reproduces the SAME Flow and
    # prefill without re-deriving them; all None for a non-form ask.
    form_pages: list[dict[str, Any]] | None = None
    form_values: dict[str, Any] | None = None
    form_options: dict[str, list[dict[str, Any]]] | None = None
    # Door-400 rejections already recovered by re-sending a fresh Flow. Bounds the
    # re-send loop (see the inbound handler's cap); starts at 0.
    rejections: int = 0
    # A form ask also carries the reverse of its schema-key → completion-payload-key map —
    # each completion-payload key (the field's human-readable label) back to its schema key —
    # so the inbound nfm_reply decode (keyed by those payload keys, as Meta relays them) maps
    # every answer key back to the schema key before coercion; all None for a non-form ask.
    form_names: dict[str, str] | None = None
    # A REACTING form ask carries its reaction triggers (field_changed/page_advanced/submitted/
    # choices) so a door-rejection re-send reproduces the SAME endpoint-driven Flow (the reacting
    # shape folds into the published-Flow cache key); None for a static form or non-form ask.
    form_reactions: dict[str, Any] | None = None
    # The ask's digression policy the shared answer ladder reads at a 400 rejection — ``"retry"``
    # (keep + notify) or ``"bridge"`` (keep + bridge the reply, no notice), carried from the
    # ``ChannelDelivery`` the channel delivered so the ladder honours it off the stored record.
    on_mismatch: str = AnswerMismatchPolicy.RETRY.value
    # The whole per-send ``wording`` catalogue (a ``Wording`` model dump) carried from the
    # ``ChannelDelivery`` the channel delivered, so a door-rejection re-send reproduces every
    # per-send text (the form's open button, title and submit label) and the shared answer ladder
    # draws the caller's refusal sentence (``wording.answer.rejected``); None when the ask carried
    # no wording.
    wording: dict[str, Any] | None = None


def correlation_key(phone_number_id: str, wa_id: str) -> str:
    """Return the opaque correlation key for a ``(phone_number_id, wa_id)`` pair — the contract store's ``key``.

    WhatsApp replies carry no thread, so the pair is the key.
    """
    return f"{phone_number_id}:{wa_id}"


def _pending_key(phone_number_id: str, wa_id: str) -> str:
    return f"channel:whatsapp:pending:{correlation_key(phone_number_id, wa_id)}"


def _flow_key(waba_id: str, flow_identity: str) -> str:
    return f"channel:whatsapp:flow:{waba_id}:{flow_identity}"


def _flow_lock_key(waba_id: str, flow_identity: str) -> str:
    return f"channel:whatsapp:flow-lock:{waba_id}:{flow_identity}"


def _flow_schema_key(waba_id: str, schema_hash: str) -> str:
    return f"channel:whatsapp:flow-schema:{waba_id}:{schema_hash}"


def _endpoint_form_key(interaction_id: str) -> str:
    return f"channel:whatsapp:endpoint-form:{interaction_id}"


def _seen_key(wamid: str) -> str:
    return f"channel:whatsapp:seen:{wamid}"


def _contact_key(phone_number_id: str, wa_id: str) -> str:
    return f"channel:whatsapp:known-contact:{phone_number_id}:{wa_id}"


def _encode_pending(question: PendingQuestion) -> str:
    return json.dumps(
        {
            "callback_url": question.callback_url,
            "timeout_at": question.timeout_at.isoformat(),
            "options": question.options,
            "interaction_id": question.interaction_id,
            "schema": question.schema,
            "question": question.question,
            "form_pages": question.form_pages,
            "form_values": question.form_values,
            "form_options": question.form_options,
            "form_names": question.form_names,
            "form_reactions": question.form_reactions,
            "on_mismatch": question.on_mismatch,
            "wording": question.wording,
            "rejections": question.rejections,
        }
    )


def _redis_settings() -> WhatsAppRedisSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = whatsapp_redis_settings()
    if not settings.redis_url:
        raise ValueError("WhatsApp channel correlation store is not configured: set CHANNEL_WHATSAPP_REDIS_URL.")
    return settings


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


async def reserve_pending(
    phone_number_id: str,
    wa_id: str,
    callback_url: str,
    timeout_at: datetime,
    options: list[str] | None = None,
    interaction_id: str | None = None,
    schema: dict[str, Any] | None = None,
    question: str | None = None,
    form_pages: list[dict[str, Any]] | None = None,
    form_values: dict[str, Any] | None = None,
    form_options: dict[str, list[dict[str, Any]]] | None = None,
    form_names: dict[str, str] | None = None,
    form_reactions: dict[str, Any] | None = None,
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY,
    wording: Wording | None = None,
) -> None:
    """Atomically reserve the pair for one question, or raise ``PendingQuestionExistsError``."""
    value = _encode_pending(
        PendingQuestion(
            callback_url=callback_url,
            timeout_at=timeout_at,
            options=options,
            interaction_id=interaction_id,
            schema=schema,
            question=question,
            form_pages=form_pages,
            form_values=form_values,
            form_options=form_options,
            form_names=form_names,
            form_reactions=form_reactions,
            on_mismatch=on_mismatch.value,
            wording=wording.model_dump(mode="json") if wording is not None else None,
        )
    )
    ttl = _remaining_seconds(timeout_at)
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        stored = await redis.set(_pending_key(phone_number_id, wa_id), value, nx=True, ex=ttl)
    if not stored:
        raise PendingQuestionExistsError(
            f"a question is already pending for the pair ({phone_number_id}, {wa_id}); "
            "one pending question per pair — answer or let it time out first"
        )


async def release_pending(phone_number_id: str, wa_id: str) -> None:
    """Drop the reservation (the send failed — the human never received the question)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_pending_key(phone_number_id, wa_id))


# Compare-and-delete in one atomic step: the pair's record is dropped only while it is held for
# ARGV[1], so a withdrawal never releases a hold a newer ask has taken on the same pair.
_RELEASE_PENDING_FOR_LUA: Final[str] = (
    "local raw = redis.call('GET', KEYS[1]) "
    "if not raw then return 0 end "
    "local rec = cjson.decode(raw) "
    "if rec.interaction_id == ARGV[1] then return redis.call('DEL', KEYS[1]) end "
    "return 0"
)


async def release_pending_for(phone_number_id: str, wa_id: str, interaction_id: str) -> bool:
    """Drop the pair's reservation ONLY if it is held for ``interaction_id``; True when released.

    A different ask's record, or no record at all, is left untouched and returns False. redis-py's
    async ``eval`` stub types a non-awaitable return; it is awaitable at runtime.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        released = await cast(
            "Awaitable[int]",
            redis.eval(_RELEASE_PENDING_FOR_LUA, 1, _pending_key(phone_number_id, wa_id), interaction_id),
        )
    return bool(released)


def _decode_pending(raw: str | bytes) -> PendingQuestion:
    data = json.loads(raw)
    return PendingQuestion(
        callback_url=data["callback_url"],
        timeout_at=datetime.fromisoformat(data["timeout_at"]),
        options=data.get("options"),
        interaction_id=data.get("interaction_id"),
        schema=data.get("schema"),
        question=data["question"],
        form_pages=data.get("form_pages"),
        form_values=data.get("form_values"),
        form_options=data.get("form_options"),
        form_names=data.get("form_names"),
        form_reactions=data.get("form_reactions"),
        on_mismatch=data.get("on_mismatch", AnswerMismatchPolicy.RETRY.value),
        wording=data.get("wording"),
        rejections=data["rejections"],
    )


async def peek_pending(phone_number_id: str, wa_id: str) -> PendingQuestion | None:
    """Read the FULL pending record WITHOUT claiming it (``GET``, no ``DEL``); ``None`` when there is none.

    The adapter-private decode surface: the shared ladder reads only the port fields
    (via :meth:`WhatsAppCorrelationStore.get_correlation`), but the channel needs the
    rich record — ``options`` to map an interactive tap to its answer, ``schema`` to
    coerce a Flow response and to know the ask is form-shaped, ``question`` +
    ``rejections`` to re-send a fresh Flow on a rejection. Non-destructive, so a
    stale/malformed reply never removes a live ask a concurrent genuine reply could
    still answer.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.get(_pending_key(phone_number_id, wa_id))
    if raw is None:
        return None
    return _decode_pending(raw)


async def bump_rejections(phone_number_id: str, wa_id: str, question: PendingQuestion) -> None:
    """Record one more door rejection on the STILL-HELD pending record, preserving its remaining budget as the TTL.

    Called after the shared ladder returned RETRY_KEPT for a form ask and the channel
    re-sent a fresh Flow: the ladder kept the reservation, so this is an in-place
    overwrite (no NX guard needed — the one-pending NX means no other reservation can
    take the pair while ours is held). A record past its deadline is left to expire.
    """
    remaining = math.ceil((question.timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return
    value = _encode_pending(replace(question, rejections=question.rejections + 1))
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_pending_key(phone_number_id, wa_id), value, ex=remaining)


class WhatsAppCorrelationStore:
    """Satisfy the contract :class:`~tai42_contract.channels.CorrelationStore` over the plugin-owned pending keys.

    Backed by the ``channel:whatsapp:pending:{pnid}:{wa_id}`` keys.
    The shared inbound-answer ladder uses ONLY this port surface (a non-destructive
    ``get`` peek and an idempotent ``release``). The channel keeps its richer decode
    state — ``options``/``schema``/``question``/``rejections`` — in the SAME stored
    record and reads it through the adapter-private :func:`peek_pending`; this port
    projects that record down to the :class:`Correlation` fields the ladder needs
    (``callback_url``, ``interaction_id``, ``ttl_deadline`` and the digression policy
    + per-send ``wording`` it reads at a 400). ``set_correlation`` (used for conformance and by any port-only caller)
    writes a minimal text-ask-shaped record — the rich deliver path uses
    :func:`reserve_pending` instead.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` NX with a ``ttl_seconds`` expiry.

        True when it was free and is now held, False when a question is already pending for the pair.
        """
        value = _encode_pending(
            PendingQuestion(
                callback_url=entry.callback_url,
                timeout_at=entry.ttl_deadline,
                interaction_id=entry.interaction_id,
                on_mismatch=entry.on_mismatch.value,
                wording=entry.wording.model_dump(mode="json") if entry.wording is not None else None,
            )
        )
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_pending_key_from_opaque(key), value, nx=True, ex=ttl_seconds)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """Return the pending record's port fields under ``key``, or ``None`` — a non-destructive peek."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            raw = await redis.get(_pending_key_from_opaque(key))
        if raw is None:
            return None
        record = _decode_pending(raw)
        return Correlation(
            callback_url=record.callback_url,
            interaction_id=record.interaction_id or "",
            ttl_deadline=record.timeout_at,
            on_mismatch=AnswerMismatchPolicy(record.on_mismatch),
            wording=Wording.model_validate(record.wording) if record.wording is not None else None,
        )

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            await redis.delete(_pending_key_from_opaque(key))


def _pending_key_from_opaque(key: str) -> str:
    return f"channel:whatsapp:pending:{key}"


# Module-level singleton: the store is stateless, so one instance serves the deliver
# and inbound paths (the channel hands it to the shared ladder explicitly).
whatsapp_correlation_store = WhatsAppCorrelationStore()


async def get_cached_flow_id(waba_id: str, flow_identity: str) -> str | None:
    """The published flow id for this ``(waba_id, flow_identity)``, or ``None``."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.get(_flow_key(waba_id, flow_identity))
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


async def cache_flow_id(waba_id: str, flow_identity: str, flow_id: str) -> None:
    """Store the published flow id with NO TTL (a published Flow persists on Meta)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_flow_key(waba_id, flow_identity), flow_id)


@asynccontextmanager
async def flow_resolve_lock(waba_id: str, flow_identity: str, *, ttl_seconds: float) -> AsyncIterator[None]:
    """Hold the redis lock that lets one send at a time resolve this Flow on this WABA.

    redis-py's ``Lock`` (``SET NX PX`` acquire, compare-and-delete release) under
    ``channel:whatsapp:flow-lock:{waba_id}:{flow_identity}``, held at most
    ``ttl_seconds`` and waited for at most as long. A wait that runs out raises a
    retryable ``ChannelDeliveryError`` (the other send is still publishing). Any other
    lock failure raises ``ChannelDeliveryError``; redis connection errors propagate.
    """
    key = _flow_lock_key(waba_id, flow_identity)
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        lock = redis.lock(key, timeout=ttl_seconds, blocking_timeout=ttl_seconds)
        try:
            acquired = await lock.acquire()
        except LockError as exc:
            raise ChannelDeliveryError(f"acquiring the WhatsApp Flow resolve lock {key} failed: {exc}") from exc
        if not acquired:
            raise ChannelDeliveryError(
                f"another send is still publishing the WhatsApp Flow for this form (waited {ttl_seconds:.0f}s)",
                retryable=True,
            )
        try:
            yield
        finally:
            try:
                await lock.release()
            except LockNotOwnedError:
                # The lock's TTL ran out first: nothing holds it, so there is nothing to
                # release, and the Flow resolved and cached under it stands without the release.
                logger.exception("flow resolve lock %s expired before release", key)
            except LockError as exc:
                raise ChannelDeliveryError(f"releasing the WhatsApp Flow resolve lock {key} failed: {exc}") from exc


@dataclass(frozen=True)
class CachedFlowForm:
    """An ask-less form's durable sidecar, keyed by ``(waba_id, schema_hash)``.

    ``schema`` and ``names`` (each completion-payload label back to its schema key) decode a reply;
    ``pages`` and ``option_fields`` (the per-send option-bearing fields, which fold into the
    schema hash) let the data endpoint check an endpoint-driven form's steps.
    """

    schema: dict[str, Any]
    names: dict[str, str]
    pages: list[dict[str, Any]] | None
    option_fields: list[str]


async def cache_flow_form(
    waba_id: str,
    schema_hash: str,
    schema: dict[str, Any],
    names: dict[str, str],
    pages: list[dict[str, Any]] | None,
    option_fields: list[str],
) -> None:
    """Store an ask-less form's sidecar under ``(waba_id, schema_hash)``, with no TTL.

    Beside the published-flow id it renders as. ``names`` maps each completion-payload key
    (the field's human-readable label) back to its schema key, so the notify reply (keyed by
    those payload keys, as Meta relays them) decodes to the schema keys and coerces by type;
    ``pages`` and ``option_fields`` are what the data endpoint reads to check an endpoint-driven
    form's steps (each fixed per schema hash, which already keys the published Flow).
    Durability is load-bearing here, not an optimization: an inbound reply carries
    only the schema HASH (inside its flow token), never the schema, so this entry can
    NOT be repopulated from the reply — a miss is permanent for every form already
    sitting in a chat, and degrades each of their replies to raw (uncoerced) values.
    The entry is content-addressed — one value per hash — so the unconditional
    overwrite on every send is idempotent. Everything is stored as one value, so a
    hit yields all of it or the read is a clean miss (no half-populated entry to reason about).
    """
    value = json.dumps({"schema": schema, "names": names, "pages": pages, "option_fields": sorted(option_fields)})
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_flow_schema_key(waba_id, schema_hash), value)


async def get_cached_flow_form(waba_id: str, schema_hash: str) -> CachedFlowForm | None:
    """Return the :class:`CachedFlowForm` cached under ``(waba_id, schema_hash)``, or ``None``.

    A miss means a reply for that schema cannot be decoded/coerced and lands with its raw
    values (see :func:`cache_flow_form`). A stored value that is not a JSON object carrying an
    object ``schema``, an object ``names``, a list (or null) ``pages`` and a list ``option_fields``
    is treated as a miss (never a crash on the webhook path).
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.get(_flow_schema_key(waba_id, schema_hash))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("cached flow form under %s is not valid JSON; treating as a miss", schema_hash)
        return None
    if not isinstance(parsed, dict):
        return None
    schema = parsed.get("schema")
    names = parsed.get("names")
    pages = parsed.get("pages")
    option_fields = parsed.get("option_fields")
    if (
        not isinstance(schema, dict)
        or not isinstance(names, dict)
        or not (pages is None or isinstance(pages, list))
        or not isinstance(option_fields, list)
    ):
        logger.warning("cached flow form under %s is not a complete form sidecar; treating as a miss", schema_hash)
        return None
    return CachedFlowForm(schema=schema, names=names, pages=pages, option_fields=option_fields)


@dataclass(frozen=True)
class EndpointForm:
    """What the data endpoint reads of an endpoint-driven form ask, keyed by its ``interaction_id``.

    ``schema`` and ``pages`` lay out its steps; ``reactions`` (the :class:`FormReactions` dump, or
    ``None``) names the consumer reactions to run after a clean shape check; ``option_fields`` the
    fields whose per-send option list replaced their schema enum; ``invalid`` the caller's per-field
    ``wording.form.fields[<name>].invalid`` sentence.
    """

    schema: dict[str, Any]
    pages: list[dict[str, Any]] | None
    reactions: dict[str, Any] | None
    option_fields: list[str]
    invalid: dict[str, str]


async def cache_endpoint_form(interaction_id: str, form: EndpointForm, timeout_at: datetime) -> None:
    """Store an endpoint-driven form ask's :class:`EndpointForm` under its ``interaction_id``, for the answer budget.

    An endpoint-driven Flow's data-endpoint request carries only the ``flow_token`` (the interaction
    id), never the schema, so this sidecar is the only place the endpoint can recover the schema
    and pages (to coerce and check a step), the declared reactions (to run after a clean check) and
    the caller's sentences (to word a refused field). Everything a screen carries forward rides the
    request itself. The entry expires with the ask's own deadline, so a stale token resolves to a
    clean miss. A record for a form past its deadline is not written (nothing left to check).
    """
    remaining = math.ceil((timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return
    value = json.dumps(
        {
            "schema": form.schema,
            "pages": form.pages,
            "reactions": form.reactions,
            "option_fields": sorted(form.option_fields),
            "invalid": form.invalid,
        }
    )
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_endpoint_form_key(interaction_id), value, ex=remaining)


async def get_endpoint_form(interaction_id: str) -> EndpointForm | None:
    """Return the :class:`EndpointForm` cached for an endpoint-driven form ask, or ``None`` on a miss.

    A miss means the form's token is unknown or its budget has expired — the data endpoint
    treats it as a no-longer-valid flow token (never a crash on the endpoint path). A stored value
    that is not a complete sidecar is a miss too, logged.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.get(_endpoint_form_key(interaction_id))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("cached endpoint form for %s is not valid JSON; treating as a miss", interaction_id)
        return None
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("schema"), dict)
        or not isinstance(parsed.get("option_fields"), list)
        or not isinstance(parsed.get("invalid"), dict)
    ):
        logger.warning("cached endpoint form for %s is not a complete form sidecar; treating as a miss", interaction_id)
        return None
    return EndpointForm(
        schema=parsed["schema"],
        pages=parsed.get("pages"),
        reactions=parsed.get("reactions"),
        option_fields=parsed["option_fields"],
        invalid=parsed["invalid"],
    )


async def drop_endpoint_form(interaction_id: str) -> None:
    """Delete the endpoint-driven form's data-endpoint sidecar (keyed by the id, so it can belong to no other ask).

    Idempotent: a missing sidecar is a no-op.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_endpoint_form_key(interaction_id))


async def already_seen(wamid: str) -> bool:
    """Whether this ``wamid`` was already handled (webhook retry or replay)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_seen_key(wamid)))


async def mark_seen(wamid: str) -> None:
    """Remember a handled ``wamid`` for the configured dedupe window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_seen_key(wamid), "1", ex=whatsapp_settings().dedupe_ttl)


async def mark_known_contact(phone_number_id: str, wa_id: str) -> None:
    """Record that this pair sent an inbound message, with a rolling window TTL.

    The TTL is ``CHANNEL_WHATSAPP_TEMPLATE_CONTACT_WINDOW_DAYS`` days and is
    refreshed on every inbound (a rolling "seen within N days"). A window of 0
    disables tracking (allowlist-only template sends) — nothing is written.
    """
    window_days = whatsapp_settings().template_contact_window_days
    if window_days <= 0:
        return
    ttl = window_days * 86_400
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_contact_key(phone_number_id, wa_id), "1", ex=ttl)


async def is_known_contact(phone_number_id: str, wa_id: str) -> bool:
    """Whether this pair sent an inbound message within the current window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_contact_key(phone_number_id, wa_id)))
