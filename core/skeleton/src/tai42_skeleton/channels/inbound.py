"""The ONE inbound-answer ladder every correlated channel shares.

Four channel plugins (Twilio, Telegram, Slack, WhatsApp) each hand-roll the same
"guest reply → forward to the interaction answer-door → interpret the door's
2xx/404/400 → bridge / release / keep" sequence over their own correlation
stores. :func:`handle_inbound_answer` is that ladder, lifted into core behind the
minimal :class:`~tai42_contract.channels.CorrelationStore` port so the POLICY
lives once here and the channels keep only their transport specifics.

The handler is engine- and channel-blind: it imports the CONTRACT (the store
port, the notification model) and reaches the running app through ``tai42_app``
only, never a channel plugin. A channel computes its own opaque correlation key,
hands the handler that key plus an :class:`InboundBridge` of the fields a bridged
turn needs, and acts on the returned :class:`InboundAnswerOutcome`.

The door is the policy authority: its 400 body carries ``retry_in_place`` (True
for every current validation rejection — the guest can answer again in place),
and the handler honors it. A future hard-mismatch 400 sets it False, and the
non-retryable seam already releases + bridges without a code change here.

The operator is told of a rejected answer through a PLATFORM EVENT, not a wired
call: core states the fact by emitting the ``interactions.answer_rejected`` topic
on the hooks manager, and a deployment decides what to do with it (a hook that
runs ``notify_user``, opens a ticket, ...) in config. Emission is best-effort —
a hooks-manager failure never fails the inbound webhook.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    AnswerForwardError,
    ChannelNotification,
    CorrelationStore,
    InboundAnswerOutcome,
    InboundBridge,
)
from tai42_kit.clients.impl.http import HttpxClient

from tai42_skeleton.interactions.settings import interactions_settings

logger = logging.getLogger(__name__)

# ``AnswerForwardError``, ``InboundAnswerOutcome`` and ``InboundBridge`` are the
# contract's shared inbound-answer types (channel plugins consume them without
# importing the skeleton); re-exported here so this module — the ladder's home — and
# its importers keep a single import site.
__all__ = [
    "ANSWER_REJECTED_EVENT_TOPIC",
    "ANSWER_REJECTED_FINAL_NOTICE",
    "ANSWER_REJECTED_RETRY_NOTICE",
    "AnswerForwardError",
    "InboundAnswerOutcome",
    "InboundBridge",
    "handle_inbound_answer",
]

# Bound the forward to the answer door — a hung door must not pin the inbound
# webhook open. 30s matches the channel plugins' http_timeout_seconds default, so
# migrating a channel onto this ladder never flips a slow-but-successful door
# response into a transport fault + provider redelivery loop.
_FORWARD_TIMEOUT_SECONDS = 30.0

# The guest-facing notice when the door rejects a still-live ask's answer and the
# guest can answer again in place. Composed with the door's human-readable reason
# so the guest knows exactly what to fix — nothing is silently swallowed. Tone
# matches the conversation bridge's other guest-safe replies.
ANSWER_REJECTED_RETRY_NOTICE = "Sorry, that didn't match what the question expects. {reason} Please try again."

# The guest-facing notice when the door rejects the answer AND the ask cannot be
# re-answered in place (a hard mismatch): the reply is bridged as a fresh turn, so
# the guest is told the question is closed rather than left waiting on a retry.
ANSWER_REJECTED_FINAL_NOTICE = "Sorry, that answer wasn't accepted and this question is now closed. {reason}"

# The platform-event topic emitted when the answer door rejects a forwarded answer.
# Core states the fact; a deployment wires a hook (topic -> a tool such as
# notify_user) in config to decide what an operator sees. Both the retryable and the
# hard-mismatch 400 variants emit it — the ``retry_in_place`` payload key distinguishes.
ANSWER_REJECTED_EVENT_TOPIC = "interactions.answer_rejected"

# Upper bound on the door's human-readable rejection reason as it rides into the
# guest notice and the operator event payload. The door names the failing field in a
# short sentence; a pathological or attacker-influenced body must not blow up a guest
# SMS/message or a persisted event frame, so the reason is bounded before either use.
# Not a UX limit — a generous abuse ceiling; a real reason is far shorter.
_REASON_MAX_CHARS = 300


def _callback_host_pinned(callback_url: str, *, channel_id: str, interaction_id: str) -> bool:
    """Whether ``callback_url``'s scheme+host+port matches the configured public base.

    A correlation's ``callback_url`` is a bearer capability THIS platform minted from
    ``INTERACTIONS_PUBLIC_BASE_URL`` (``helper.py`` builds it as
    ``{public_base_url}/api/interactions/callback/{ticket}``). Before the handler POSTs
    a guest's answer to it, the host is re-checked against the current public base: a
    stored entry whose host no longer matches — a poisoned or stale store record, or a
    misconfiguration — must NEVER receive the forwarded answer, because that POST would
    ship the guest's reply to an attacker-chosen host. The offending host is logged
    loudly (never the full URL: it embeds the callback ticket).

    When no public base is configured the platform cannot have minted a callback and
    the check cannot run; it is skipped with a warning rather than silently passing a
    spoof — a correlated channel always sets a public base in practice.
    """
    public_base = interactions_settings().public_base_url
    if public_base is None:
        logger.warning(
            "inbound: INTERACTIONS_PUBLIC_BASE_URL is unset; cannot host-pin the callback for interaction %s "
            "on channel %r — forwarding without the host check",
            interaction_id,
            channel_id,
        )
        return True
    base = urlparse(public_base)
    target = urlparse(callback_url)
    if (
        target.scheme == base.scheme
        and (target.hostname or "").lower() == (base.hostname or "").lower()
        and target.port == base.port
    ):
        return True
    logger.error(
        "inbound: correlation for interaction %s on channel %r carries a callback host %r that is not the "
        "configured public base host %r — releasing and treating as no correlation (possible poisoned store)",
        interaction_id,
        channel_id,
        target.netloc,
        base.netloc,
    )
    return False


async def _forward_answer(callback_url: str, answer: Any) -> httpx.Response:
    """POST ``{"answer": <value>}`` to the interaction's callback door and return
    its response; the caller applies the status policy. Mirrors each channel's
    ``_forward_answer`` — a bounded-timeout app-pooled httpx client."""
    async with tai42_app.clients.client_ctx(HttpxClient, timeout=_FORWARD_TIMEOUT_SECONDS) as client:
        return await client.post(callback_url, json={"answer": answer})


async def _bridge(bridge: InboundBridge) -> None:
    """Hand the reply to the conversation bridge as a fresh turn (the reply is not,
    or no longer, an answer). Idempotent on ``(channel, provider_message_id)`` at
    the conversation seam, so a provider redelivery does not double-bridge."""
    await tai42_app.conversations.accept(
        channel=bridge.channel_id,
        our_identity=bridge.our_identity,
        client_address=bridge.client_address,
        cap_key=bridge.cap_key,
        text=bridge.bridge_text,
        provider_message_id=bridge.provider_message_id,
    )


async def _notify_guest(bridge: InboundBridge, message: str) -> None:
    """Send the guest a rejection notice on the same channel the ask went out on.

    Best-effort: a notify failure is logged and swallowed so it never turns a
    handled rejection into a lost webhook — the operator event still fires and the
    outcome still returns. Sends from the ask's own identity to the guest address.
    """
    try:
        channel = tai42_app.channels.get(bridge.channel_id)
        await channel.notify(
            ChannelNotification(message=message, recipient=bridge.client_address, sender_identity=bridge.our_identity)
        )
    except Exception:
        logger.warning(
            "inbound: failed to send the answer-rejected notice to %s on channel %r; continuing",
            bridge.client_address,
            bridge.channel_id,
            exc_info=True,
        )


async def _emit_answer_rejected(
    bridge: InboundBridge,
    *,
    interaction_id: str,
    error: str,
    field: str | None,
    retry_in_place: bool,
) -> None:
    """Emit the ``interactions.answer_rejected`` platform event ONCE for a
    door-rejected answer.

    Core states the fact; a deployment wires a hook on this topic (e.g. a
    ``notify_user`` tool) to decide what an operator sees. Best-effort: a
    hooks-manager failure is logged and swallowed so it never fails the inbound
    webhook — the guest notice and the ladder outcome are unaffected.
    """
    # Local import: reach the hooks-manager accessor only when emitting, keeping
    # this module's load-time import surface to the contract + kit (the codebase's
    # function-local-import pattern for avoiding an import cycle across packages).
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {
        "channel": bridge.channel_id,
        "interaction_id": interaction_id,
        "client_address": bridge.client_address,
        "our_identity": bridge.our_identity,
        "reason": error,
        "field": field,
        "retry_in_place": retry_in_place,
    }
    try:
        await get_hooks_manager().on_event(topic=ANSWER_REJECTED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "inbound: failed to emit %r for the rejected answer on channel %r interaction %s",
            ANSWER_REJECTED_EVENT_TOPIC,
            bridge.channel_id,
            interaction_id,
            exc_info=True,
        )


async def handle_inbound_answer(
    *,
    channel_id: str,
    correlation_key: str,
    answer: Any,
    store: CorrelationStore,
    bridge: InboundBridge,
) -> InboundAnswerOutcome:
    """Resolve one inbound guest reply against its pending ask — the shared ladder.

    ``correlation_key`` is the channel's opaque key for this address; ``answer`` is
    the value forwarded to the door as ``{"answer": answer}``; ``store`` is the
    channel's :class:`~tai42_contract.channels.CorrelationStore`; ``bridge`` carries
    the fields a bridged turn needs.

    The ladder:

    * No pending ask on the key -> :attr:`~InboundAnswerOutcome.NO_CORRELATION`
      with NO side effect (the caller bridges the reply as a normal turn).
    * The door accepts (2xx) -> release the correlation, return
      :attr:`~InboundAnswerOutcome.FORWARDED`.
    * The door returns 404 (the ask was withdrawn/expired/cancelled, including the
      post-cascade thread-delete case) -> release, bridge the reply as a fresh
      turn, return :attr:`~InboundAnswerOutcome.BRIDGED`.
    * The door returns 400 (the LIVE ask rejected this answer's format) -> read the
      door's ``retry_in_place`` (default True):

      - True: KEEP the correlation, notify the guest what's expected, fire ONE
        operator alert, return :attr:`~InboundAnswerOutcome.RETRY_KEPT`. The guest
        can answer again in place; nothing is silent.
      - False (a future hard mismatch): release, notify the guest the question is
        closed, fire the operator alert, bridge the reply, return
        :attr:`~InboundAnswerOutcome.BRIDGED`.

    * Anything else (401/413/5xx, or a transport fault) -> do NOT release; raise
      :class:`AnswerForwardError` so the channel's webhook redelivery re-runs the
      ladder. The answer is never silently lost.
    """
    entry = await store.get_correlation(correlation_key)
    if entry is None:
        # Side-effect-free miss: no pending ask on this key, so the caller bridges
        # the reply as a normal turn. The handler touches nothing.
        return InboundAnswerOutcome.NO_CORRELATION

    if not _callback_host_pinned(entry.callback_url, channel_id=channel_id, interaction_id=entry.interaction_id):
        # The stored callback host is not this platform's configured public base:
        # never POST the guest's answer to it. Release the poisoned/stale reservation
        # and treat the reply as uncorrelated so the caller bridges it as a normal turn
        # (never lost) — the loud log is emitted by the pin check.
        await store.release_correlation(correlation_key)
        return InboundAnswerOutcome.NO_CORRELATION

    try:
        forwarded = await _forward_answer(entry.callback_url, answer)
    except httpx.HTTPError as exc:
        # Transport fault forwarding to the door — indeterminate. Keep the
        # correlation and fail loudly so the webhook redelivery re-resolves it.
        raise AnswerForwardError(f"forwarding the answer to the door failed: {exc}") from exc

    status = forwarded.status_code
    if status // 100 == 2:
        await store.release_correlation(correlation_key)
        return InboundAnswerOutcome.FORWARDED

    if status == 404:
        # Terminal: the ask is gone (withdrawn/expired/cancelled/thread-deleted).
        # The dead ticket cannot accept the answer, but the guest's reply must
        # never be lost — release and bridge it as an ordinary conversation turn.
        logger.warning(
            "inbound: answer door returned terminal 404 for interaction %s on channel %r; the ask is gone — "
            "bridging the reply into the conversation instead of dropping it",
            entry.interaction_id,
            channel_id,
        )
        await store.release_correlation(correlation_key)
        await _bridge(bridge)
        return InboundAnswerOutcome.BRIDGED

    if status == 400:
        # THE SPLIT: the LIVE ask rejected this answer's format. The door is the
        # policy authority — its structured body decides whether the ask is
        # re-answerable in place. Default RETRYABLE (text/select/confirm/form
        # wrong-format cases): the guest can answer again on the same ask.
        error, field, retry_in_place = _parse_rejection(forwarded)
        if retry_in_place:
            # KEEP the correlation so the guest's next reply resolves the same ask.
            reason = error or "The answer wasn't in the expected format."
            await _notify_guest(bridge, ANSWER_REJECTED_RETRY_NOTICE.format(reason=reason))
            await _emit_answer_rejected(
                bridge,
                interaction_id=entry.interaction_id,
                error=error,
                field=field,
                retry_in_place=True,
            )
            logger.warning(
                "inbound: answer door rejected the answer for interaction %s on channel %r (400, retry-in-place); "
                "correlation kept so the guest can answer again",
                entry.interaction_id,
                channel_id,
            )
            return InboundAnswerOutcome.RETRY_KEPT

        # Non-retryable hard mismatch: the ask cannot take this answer and cannot be
        # re-answered in place. Release, tell the guest the question is closed, alert
        # the operator, and bridge the reply as a fresh turn.
        reason = error or "The answer wasn't accepted."
        await store.release_correlation(correlation_key)
        await _notify_guest(bridge, ANSWER_REJECTED_FINAL_NOTICE.format(reason=reason))
        await _emit_answer_rejected(
            bridge,
            interaction_id=entry.interaction_id,
            error=error,
            field=field,
            retry_in_place=False,
        )
        logger.warning(
            "inbound: answer door rejected the answer for interaction %s on channel %r (400, non-retryable); "
            "correlation released and the reply bridged",
            entry.interaction_id,
            channel_id,
        )
        await _bridge(bridge)
        return InboundAnswerOutcome.BRIDGED

    # 401/413/5xx ambient failure — keep the correlation and fail loudly so the
    # channel's webhook redelivery re-runs the ladder.
    raise AnswerForwardError(f"interactions answer door rejected the answer: HTTP {status}: {forwarded.text[:500]}")


def _parse_rejection(response: httpx.Response) -> tuple[str, str | None, bool]:
    """Read the door's structured 400 body: ``{"error": msg, "field": path,
    "retry_in_place": bool}``. Missing/malformed fields degrade safely — an
    unreadable body means an empty reason and the default retry-in-place True, so a
    rejection is never mistaken for a hard mismatch."""
    try:
        body = response.json()
    except ValueError:
        return "", None, True
    if not isinstance(body, dict):
        return "", None, True
    error = body.get("error")
    field = body.get("field")
    retry_in_place = body.get("retry_in_place", True)
    # Bound the reason before it rides into the guest notice and the persisted event
    # payload — a pathological door body must not blow up a guest message or a frame.
    return (
        error[:_REASON_MAX_CHARS] if isinstance(error, str) else "",
        field if isinstance(field, str) else None,
        bool(retry_in_place),
    )
