"""Persisting a new question: build the durable ``InteractionRequest`` and, on a
fresh connection, substitute media, reserve the concurrency slot and write the
question to the store."""

from __future__ import annotations

from typing import Any, Literal

from tai42_contract.interactions import AnswerFormat, InteractionRequest, MediaItem
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.media import substitute_media
from tai42_skeleton.interactions.origin import get_interaction_origin
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore

from .errors import InteractionLimitError
from .park import AsyncParkBinding
from .timing import CallbackTicket, DeadlineWindow


def build_request(
    *,
    interaction_id: str,
    group: str,
    question: str,
    fmt: AnswerFormat,
    format_payload: dict[str, Any] | None,
    on_mismatch: Any,
    mismatch_notice: str | None,
    reply_to: str,
    window: DeadlineWindow,
    sensitive: bool,
    channel: str | None,
    recipient: str | None,
    audience: str | None,
    stored_media: list[MediaItem] | None,
    park_binding: AsyncParkBinding,
    mode: Literal["sync", "async"],
    expiry_at: Any,
) -> InteractionRequest:
    """Build the durable ``InteractionRequest`` for the question — its answer format +
    payload, the digression policy/notice, the deadline window, the delivery
    channel/recipient/audience, the stored media, and (for an async park) the resolved
    continuation binding."""
    return InteractionRequest(
        interaction_id=interaction_id,
        group_id=group,
        question=question,
        answer_format=fmt,
        format_payload=format_payload,
        # The per-ask digression policy + custom retry notice ride the durable record
        # for attribution; the ladder reads them off the Correlation the channel parks.
        on_mismatch=on_mismatch,
        mismatch_notice=mismatch_notice,
        reply_to=reply_to,
        created_at=window.created_at,
        timeout_at=window.timeout_at,
        sensitive=sensitive,
        channel=channel,
        recipient=recipient,
        audience=audience,
        # Origin of the raising run (a background tool-run id), read from the
        # contextvar the run binds; None outside a bound tool run.
        origin=get_interaction_origin(),
        media=stored_media,
        # Async park: the sentinel-returning discipline plus the generic continuation
        # resolved up front (both None for a sync ask). The model's own validator
        # enforces continuation-iff-async.
        mode=mode,
        continuation_tool=park_binding.continuation_tool,
        continuation_identity=park_binding.continuation_identity,
        continuation_state_context=park_binding.continuation_state_context,
        expiry_at=expiry_at,
    )


async def persist_question(
    store: InteractionStore,
    settings: InteractionsSettings,
    window: DeadlineWindow,
    callback: CallbackTicket | None,
    park_binding: AsyncParkBinding,
    *,
    interaction_id: str,
    group: str,
    question: str,
    fmt: AnswerFormat,
    format_payload: dict[str, Any] | None,
    on_mismatch: Any,
    mismatch_notice: str | None,
    reply_to: str,
    sensitive: bool,
    channel: str | None,
    recipient: str | None,
    audience: str | None,
    media: list[MediaItem | dict[str, Any]] | None,
    mode: Literal["sync", "async"],
    expiry_at: Any,
) -> list[MediaItem] | None:
    """Open the persist connection, substitute media by reference, build the request,
    reserve the concurrency slot under ``max_concurrent`` (raising
    ``InteractionLimitError`` at the cap) and write it. Returns the stored media list
    (the same items the durable record and any channel delivery carry)."""
    from tai42_skeleton.interactions import helper

    async with helper.client_ctx(RedisClient, settings.redis) as r:
        # A data:image is decoded once and stored BY REFERENCE before the request is
        # built, so the durable record carries a served reference the inbox renders,
        # never the inline bytes. The media keys start at ``idle_ttl`` and ``add``
        # extends them to the group horizon. https/link items pass through unchanged.
        # A channel ask must carry an ABSOLUTE served url (a relative reference is
        # unfetchable off-origin), so pass ``base_url`` when a channel is set
        # (``public_base_url`` is already hard-required whenever a channel is set);
        # an inbox-only ask keeps the relative same-origin url.
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
        request = build_request(
            interaction_id=interaction_id,
            group=group,
            question=question,
            fmt=fmt,
            format_payload=format_payload,
            on_mismatch=on_mismatch,
            mismatch_notice=mismatch_notice,
            reply_to=reply_to,
            window=window,
            sensitive=sensitive,
            channel=channel,
            recipient=recipient,
            audience=audience,
            stored_media=stored_media,
            park_binding=park_binding,
            mode=mode,
            expiry_at=expiry_at,
        )
        ticket = callback.ticket if callback is not None else None
        ticket_ttl = callback.ticket_ttl if callback is not None else None
        # Concurrency guard (all formats). ``reserve_open_slot`` prunes stale open
        # members, refuses at the cap, and reserves this question's open-index member
        # in ONE atomic step. A reserved slot means ``add`` must skip re-adding it.
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
                continuation_fingerprint=park_binding.continuation_fingerprint,
                expiry_ttl_margin_seconds=window.park_ttl_margin_seconds,
                thread_id=park_binding.park_thread_id,
            )
        else:
            await store.add(
                r,
                request,
                settings.idle_ttl_seconds,
                ticket=ticket,
                ticket_ttl=ticket_ttl,
                continuation_fingerprint=park_binding.continuation_fingerprint,
                expiry_ttl_margin_seconds=window.park_ttl_margin_seconds,
                thread_id=park_binding.park_thread_id,
            )
    return stored_media
