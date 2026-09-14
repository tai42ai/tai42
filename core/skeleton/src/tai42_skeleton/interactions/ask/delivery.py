"""Channel delivery with bounded retry: build the delivery frame, deliver within the
ask's budget retrying typed-retryable failures with backoff, and on terminal failure
prune the question and state the loud abandonment event."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tai42_contract.channels import Channel, ChannelDelivery, ChannelDeliveryError
from tai42_contract.interactions import AnswerFormat, FormData, FormPage, MediaItem

from tai42_skeleton.channels.send_span import send_span
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore, PruneResult
from tai42_skeleton.tools.turn_budget import mark_parked_question

from .timing import DeadlineWindow

logger = logging.getLogger(__name__)


def retry_delay(exc: BaseException, attempt: int, remaining: float, settings: InteractionsSettings) -> float | None:
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


async def prune(
    settings: InteractionsSettings, store: InteractionStore, interaction_id: str, group_id: str
) -> PruneResult:
    """Prune an abandoned question on its OWN connection — never the cancelled
    BLPOP connection, which is not safely reusable for a WATCH/MULTI. Returns
    ``prune_pending``'s result: ``"pruned"`` when it pruned, ``"answered"`` when an
    answer was already recorded, ``"gone"`` when the state key was already
    missing/expired."""
    from tai42_kit.clients.impl.redis import RedisClient

    from tai42_skeleton.interactions import helper

    async with helper.client_ctx(RedisClient, settings.redis) as conn:
        return await store.prune_pending(conn, interaction_id, group_id)


async def emit_delivery_failed(*, channel: str, interaction_id: str, recipient: str | None, error: str) -> None:
    """Emit the ``interactions_delivery_failed`` platform event ONCE when a channel
    delivery of a question has been TERMINALLY abandoned — the question pruned, nothing
    answered — alongside the unchanged raise that propagates the failure.

    Core states the fact; a deployment wires a hook on this topic (e.g. a ``notify_user``
    tool, a ticket) in config to decide what an operator sees. Best-effort: a
    hooks-manager failure is logged and swallowed so the event can never turn a loud
    delivery failure into a different error — the raise path stays exactly as it was.
    """
    # Local import: reach the hooks-manager accessor only when emitting, mirroring the
    # inbound ladder's pattern (keeps a module-load import edge out of this module and
    # avoids an import cycle across packages).
    from tai42_skeleton.hooks.cache import get_hooks_manager
    from tai42_skeleton.interactions import helper

    payload = {
        "channel": channel,
        "interaction_id": interaction_id,
        "recipient": recipient,
        "error": error,
    }
    try:
        await get_hooks_manager().on_event(topic=helper.DELIVERY_FAILED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "ask_user: failed to emit %r for the abandoned delivery on channel %r interaction %s",
            helper.DELIVERY_FAILED_EVENT_TOPIC,
            channel,
            interaction_id,
            exc_info=True,
        )


def build_delivery_frame(
    *,
    interaction_id: str,
    recipient: str | None,
    question: str,
    fmt: AnswerFormat,
    options: list[str] | None,
    format_payload: dict[str, Any] | None,
    stored_media: list[MediaItem] | None,
    on_mismatch: Any,
    mismatch_notice: str | None,
    callback_url: str,
    timeout_at: Any,
) -> ChannelDelivery:
    """Build the frozen ``ChannelDelivery`` frame reused across delivery attempts: the
    question + format + suggested options, a form's normalized schema/prefill/pages, the
    display media (absolute served refs), the digression policy/notice, the callback URL
    and the stored deadline."""
    payload = format_payload or {}
    is_form = fmt is AnswerFormat.FORM
    return ChannelDelivery(
        interaction_id=interaction_id,
        recipient=recipient,
        question=question,
        answer_format=fmt.value,
        options=options,
        # For a form the normalized schema rides the delivery; the payload builder
        # already required and normalized it above (before any persist).
        schema=payload.get("schema") if is_form else None,
        # A form's per-send prefill/options and stepped pages ride the delivery too,
        # re-parsed from the payload the request already validated — the channel plugin
        # renders them into its own form surface. None for every other format.
        data=(FormData.model_validate(payload["data"]) if is_form and payload.get("data") is not None else None),
        pages=(
            [FormPage.model_validate(page) for page in payload["pages"]]
            if is_form and payload.get("pages") is not None
            else None
        ),
        # The question's display media rides the delivery too — the SAME stored items,
        # here with any data: image substituted to an ABSOLUTE served reference, so a
        # vendor can fetch it off-origin. None when the ask carried no media.
        media=stored_media,
        # The ladder's authoritative path: the channel copies these onto the
        # ``Correlation`` it parks, and the shared inbound-answer ladder reads them at
        # the 400 decision (bridge the digression / send the custom retry notice).
        on_mismatch=on_mismatch,
        mismatch_notice=mismatch_notice,
        callback_url=callback_url,
        timeout_at=timeout_at,
    )


async def deliver_with_retry(
    channel_obj: Channel,
    delivery_frame: ChannelDelivery,
    settings: InteractionsSettings,
    store: InteractionStore,
    window: DeadlineWindow,
    *,
    channel: str,
    recipient: str | None,
    interaction_id: str,
    group: str,
    question: str,
    sensitive: bool,
) -> None:
    """Deliver the question within the ask's budget, retrying typed-retryable failures
    with exponential backoff (all inside the one monotonic ``deadline`` the answer wait
    shares). On terminal failure prune the question and re-raise loudly — UNLESS the
    reply already landed first (``answered``/``gone``), in which case fall through so
    the recorded answer is returned by the wait that follows.

    Each ``deliver`` is ONE send attempt; a failure the plugin typed as ``retryable``
    (a medium 5xx, a rate limit, a transport fault, a hung send) is re-attempted up to
    ``delivery_max_attempts``, and everything else fails on the first try. A deliver
    call that does not return within the remaining budget is itself a typed, retryable
    delivery failure — an unbounded await would block the caller forever with the
    question persisted."""
    loop = asyncio.get_running_loop()
    attempt = 0
    retry_in: float | None = None
    while True:
        attempt += 1
        try:
            if retry_in is not None:
                await asyncio.sleep(retry_in)
            # Each attempt is bounded by what is left of the budget: a plugin that
            # consumes it is hung. Tier 1 of the send-outcome monitoring layer: one
            # ``send:<channel>`` span per delivery ATTEMPT (the retry loop makes
            # distinct sends). ``deliver`` returns None on success — no correlatable
            # provider id at this seam — so no tier-2 receipt index is written here.
            attempt_timeout = window.deadline - loop.time()
            with send_span(channel, recipient=recipient, attempt=attempt):
                try:
                    await asyncio.wait_for(channel_obj.deliver(delivery_frame), timeout=attempt_timeout)
                except TimeoutError as exc:
                    raise ChannelDeliveryError(
                        f"channel {channel!r} delivery timed out after {attempt_timeout:.1f}s "
                        f"of the ask's {window.budget}s budget (interaction {interaction_id})",
                        retryable=True,
                    ) from exc
        except BaseException as exc:
            retry_in = retry_delay(exc, attempt, window.deadline - loop.time(), settings)
            if retry_in is not None:
                # Intermediate failure: the question stays open for the next attempt —
                # never pruned here, never silent.
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
            if await _prune_and_report_failure(
                settings,
                store,
                exc,
                interaction_id=interaction_id,
                group=group,
                question=question,
                sensitive=sensitive,
                channel=channel,
                recipient=recipient,
            ):
                raise
        break


async def _prune_and_report_failure(
    settings: InteractionsSettings,
    store: InteractionStore,
    exc: BaseException,
    *,
    interaction_id: str,
    group: str,
    question: str,
    sensitive: bool,
    channel: str,
    recipient: str | None,
) -> bool:
    """Handle a delivery failure with no retry left: prune the question, then decide
    whether the caller must re-raise. Returns ``True`` when the failure is terminal
    (pruned, or a non-``Exception`` like cancellation — the caller re-raises loudly),
    ``False`` when a recorded answer beat the failure (``answered``/``gone`` — fall
    through to the answer wait). On a pruned cancellation the parked-question turn
    budget is marked; on a pruned genuine failure the abandonment event is stated."""
    result = await prune(settings, store, interaction_id, group)
    if result == "pruned" or not isinstance(exc, Exception):
        # Pruned → nothing was answered; propagate the failure loudly. A non-Exception
        # (asyncio.CancelledError mid-send or mid-backoff, SystemExit) ALWAYS
        # propagates — cancellation is never retried and never swallowed.
        if isinstance(exc, asyncio.CancelledError):
            mark_parked_question(exc, interaction_id, question, sensitive)
        elif result == "pruned" and isinstance(exc, Exception):
            # Terminal delivery abandonment: state the fact as a best-effort platform
            # event that RIDES ALONGSIDE the raise — never a cancellation (handled
            # above), never the answered/gone fall-through, never a replacement for the
            # error path.
            await emit_delivery_failed(
                channel=channel,
                interaction_id=interaction_id,
                recipient=recipient,
                error=str(exc),
            )
        return True
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
    return False
