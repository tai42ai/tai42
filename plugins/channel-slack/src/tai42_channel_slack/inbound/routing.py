"""Shared answer/bridge routing for both inbound doors.

The correlated-answer ladder (a threaded reply or option tap resolved against its
pending ask) and the conversation bridge (any uncorrelated human message entering
as a fresh turn), plus the recipient allowlist both doors gate against.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import InboundAnswerOutcome, InboundBridge
from tai42_contract.conversations import BlankInboundTextError

from tai42_channel_slack.correlation import slack_thread_correlation_store
from tai42_channel_slack.settings import SlackSettings

logger = logging.getLogger(__name__)

# The shared ladder's outcome -> the Events door's JSON ack status. Every non-miss
# outcome is a 2xx Slack accepts; the string is informational (Slack only needs the
# 2xx). A NO_CORRELATION miss is handled by the caller (it bridges).
_EVENTS_ACK = {
    InboundAnswerOutcome.FORWARDED: "forwarded",
    InboundAnswerOutcome.RETRY_KEPT: "rejected",
    InboundAnswerOutcome.BRIDGED: "bridged",
    InboundAnswerOutcome.BRIDGED_KEPT: "bridged",
}


def _recipients(settings: SlackSettings) -> set[str]:
    """The channels a threaded reply or option tap may answer an ask from.

    The configured allowlist plus the default recipient when one is set.
    """
    recipients = set(settings.allowed_recipients)
    if settings.default_recipient is not None:
        recipients.add(settings.default_recipient)
    return recipients


async def _bridge(
    our_identity: str | None,
    channel: object,
    text: object,
    event_id: str,
    *,
    params: dict[str, str] | None = None,
) -> Response:
    """Hand one uncorrelated human message to the conversation bridge.

    ``our_identity`` (the bot user id) is required here — a bridge route with it
    unset is operator misconfig, raised loudly. A message with no text/channel, or
    with whitespace-only text, is nothing to bridge and is acked. ``params`` is the
    optional opaque channel enrichment the turn carries (e.g. ``reply_id`` from a tapped
    reply option); ``None`` for a plain message. No route bound acks with a debug log; an
    infrastructure failure propagates so Slack redelivers.
    """
    if not isinstance(text, str) or not text or not isinstance(channel, str) or not channel:
        return JSONResponse({"status": "ignored"})
    if our_identity is None:
        raise ValueError("the slack channel bridge is not configured: set CHANNEL_SLACK_BOT_USER_ID")
    try:
        await tai42_app.conversations.accept(
            channel="slack",
            our_identity=our_identity,
            client_address=channel,
            # The provider attests the channel id, so it is both the conversation identity
            # and the party the turn cap holds accountable.
            cap_key=channel,
            text=text,
            provider_message_id=event_id,
            params=params,
        )
    except BlankInboundTextError:
        logger.debug("slack inbound: blank text for channel=%s; acking", channel)
        return JSONResponse({"status": "ignored"})
    except LookupError:
        logger.debug("slack inbound: no conversation route for channel=%s; acking", channel)
        return JSONResponse({"status": "ignored"})
    return JSONResponse({"status": "accepted"})


async def _resolve_answer(
    thread_ts: str,
    text: object,
    our_identity: str | None,
    channel: str,
    event_id: str,
    *,
    params: dict[str, str] | None = None,
) -> Response:
    """Resolve a correlated threaded reply against its pending ask via the ONE shared ladder, or bridge on a miss.

    The ask is peeked first (channel-side) so a correlated reply that carries no text is
    the loud error it has always been (raise -> 500, Slack retries then logs) rather than
    an ask resolved with ``None`` — while an uncorrelated thread reply bridges. A live
    ask hands the text to the ladder (a threaded reply is re-answerable in place, so the
    ladder owns the retry notice, ``owns_retry_notice=False``). ``event_id`` is the
    bridge's dedupe key at the conversation seam. An ``AnswerForwardError`` (401/5xx /
    transport fault) propagates so the outer guard frees the dedupe claim and Slack's
    retry re-runs the ladder.
    """
    entry = await slack_thread_correlation_store.get_correlation(thread_ts)
    if entry is None:
        # No pending ask on this thread (expired / never ours) — bridge like any message.
        return await _bridge(our_identity, channel, text, event_id, params=params)
    if not isinstance(text, str) or not text:
        raise ValueError(f"correlated slack reply in thread {thread_ts} carries no text")

    # The forward path needs no bot user id — it rides only into the bridge context the
    # ladder uses ONLY on a terminal 404 (BRIDGED). An unset id there degrades that rare
    # bridge, never the common forward; the NO_CORRELATION bridge path still requires it.
    # A threaded reply is re-answered in place, so the ladder owns the retry notice and
    # the door reason it carries back is ignored here.
    result = await tai42_app.channels.handle_inbound_answer(
        channel_id="slack",
        correlation_key=thread_ts,
        answer=text,
        store=slack_thread_correlation_store,
        bridge=InboundBridge(
            channel_id="slack",
            our_identity=our_identity or "",
            client_address=channel,
            # The provider attests the channel id, so it is both the conversation
            # identity and the party the turn cap holds accountable.
            cap_key=channel,
            provider_message_id=event_id,
            bridge_text=text,
            owns_retry_notice=False,
            # Opaque enrichment (e.g. ``reply_id`` from a tapped reply option) the ladder
            # threads to the ask callback and, when the reply is bridged instead, onto the
            # turn — so a tap's author-set id is never dropped on either arm.
            params=params,
        ),
    )
    if result.outcome is InboundAnswerOutcome.NO_CORRELATION:
        return await _bridge(our_identity, channel, text, event_id, params=params)
    return JSONResponse({"status": _EVENTS_ACK[result.outcome]})
