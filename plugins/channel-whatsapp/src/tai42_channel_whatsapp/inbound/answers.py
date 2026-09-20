"""Correlated-answer ladder and the conversation bridge.

A reply that matches a pending question is resolved through the ONE shared
inbound-answer ladder; a message with no route, or a correlation miss, enters the
conversation bridge as a fresh turn.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    InboundAnswerOutcome,
    InboundBridge,
)
from tai42_contract.conversations import BlankInboundTextError, validate_entry_params
from tai42_contract.interactions.models import LocationElement, MediaItem
from tai42_kit.utils.data.form_text import render_form_text

from tai42_channel_whatsapp.correlation import (
    PendingQuestion,
    correlation_key,
    mark_seen,
    whatsapp_correlation_store,
)

logger = logging.getLogger(__name__)


def _render_answer_for_bridge(answer: str | dict[str, Any], pending: PendingQuestion) -> str:
    """A faithful, ALWAYS non-empty text rendering of a correlated reply for the conversation bridge.

    Used when the interaction is terminally gone. A typed reply or a resolved
    select tap is already its human-readable string; a
    completed Flow form renders through :func:`render_form_text` against the
    pending ask's schema.
    """
    if isinstance(answer, str):
        return answer
    return render_form_text(answer, pending.schema)


async def _resolve_answer(
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    answer: str | dict[str, Any],
    pending: PendingQuestion,
    params: dict[str, str] | None = None,
) -> None:
    """Resolve a correlated reply against its pending ask via the ONE shared ladder.

    ``answer`` is already final: a typed reply's stripped body, the option text an
    interactive tap resolved to, or the coerced dict of a completed form (Flow). The
    ladder forwards it, interprets the door's outcome over the plugin's
    :class:`CorrelationStore`, and returns the outcome the channel maps:

    * ``NO_CORRELATION`` — the ask expired between the channel's decode-peek and the
      ladder's own peek: bridge the reply as a fresh turn (never lost).
    * ``RETRY_KEPT`` on a FORM ask — the channel owns the correction surface
      (``owns_retry_notice=True``, so the ladder sent NO participant notice): re-send a fresh
      Flow carrying the door's own reason so the participant can answer again in place.
    * ``FORWARDED`` / ``BRIDGED`` / ``RETRY_KEPT`` on a text/select ask (the ladder sent
      the generic notice) — mark the wamid seen so a redelivery is not re-processed.

    An :class:`AnswerForwardError` (401/413/5xx / transport fault) propagates so Meta
    redelivers and re-runs the ladder; the wamid is NOT marked seen on that raise.
    """
    # Deferred import: ``forms`` depends on this module (the ladder + bridge), so the
    # form-recovery back-edge is imported here to keep the package import graph acyclic.
    from tai42_channel_whatsapp.inbound.forms import _recover_form_rejection

    is_form = pending.schema is not None
    bridge_text = _render_answer_for_bridge(answer, pending)
    result = await tai42_app.channels.handle_inbound_answer(
        channel_id="whatsapp",
        correlation_key=correlation_key(phone_number_id, wa_id),
        answer=answer,
        store=whatsapp_correlation_store,
        bridge=InboundBridge(
            channel_id="whatsapp",
            our_identity=phone_number_id,
            client_address=wa_id,
            # The provider attests the wa_id, so it is both the conversation identity
            # and the party the turn cap holds accountable.
            cap_key=wa_id,
            provider_message_id=wamid,
            bridge_text=bridge_text,
            # A form ask's correction surface is a re-opened Flow the channel renders
            # off RETRY_KEPT; a text/select ask is re-answered in place, so core owns
            # its notice. Setting this per ask-shape keeps the participant messaged exactly
            # once either way.
            owns_retry_notice=is_form,
        ),
    )
    if result.outcome is InboundAnswerOutcome.NO_CORRELATION:
        # The fallback IS the bridge path, so it carries the same params the caller's
        # own bridge branch would have carried (the correlated forward takes none).
        await _bridge_inbound(phone_number_id, wa_id, bridge_text, wamid, params=params)
        return
    if result.outcome is InboundAnswerOutcome.RETRY_KEPT and is_form:
        await _recover_form_rejection(phone_number_id, wa_id, wamid, pending, result.retry_reason)
        return
    await mark_seen(wamid)


async def _bridge_inbound(
    phone_number_id: str,
    wa_id: str,
    text: str,
    wamid: str,
    form: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> None:
    """Route an uncorrelated inbound message into the conversation bridge.

    ``our_identity`` = phone_number_id, ``client_address`` = wa_id (verbatim);
    ``form`` is an ask-less form submission's structured copy, riding beside its
    rendered ``text``; ``params`` are the channel's opaque entry-params (reply ids,
    referral, reply-to context, media identity — see the ``params`` vocabulary block)
    forwarded verbatim to the tool target's payload. ``attachments`` (typed participant media) and
    ``location`` (a shared geographic point) are the structured inbound content that lands on
    a tool target's payload under the stable ``attachments``/``location`` keys; inbound media
    currently carries none (see the INBOUND MEDIA design note in ``rich_content``), while an
    inbound location passes its typed :class:`LocationElement`. A message with no route bound,
    or with blank text (an empty interactive title), is logged and skipped; a retryable
    overflow or infrastructure failure propagates as a 5xx so Meta redelivers.

    ``params`` are validated against the contract's transport bounds HERE before accept: a
    bound violation (which would otherwise 5xx and have Meta redeliver the same poison
    message forever) drops the whole params set and bridges the turn without it — the
    participant's message is never lost to a params bound. The refusal names the bound/key, never
    an opaque value.
    """
    if params:
        try:
            validate_entry_params(params)
        except ValueError as exc:
            logger.warning("whatsapp inbound %s params rejected (%s); bridging without params", wamid, exc)
            params = None
    try:
        await tai42_app.conversations.accept(
            channel="whatsapp",
            our_identity=phone_number_id,
            client_address=wa_id,
            # The provider attests the wa_id, so it is both the conversation identity and
            # the party the turn cap holds accountable.
            cap_key=wa_id,
            text=text,
            provider_message_id=wamid,
            params=params,
            form=form,
            attachments=attachments,
            location=location,
        )
    except BlankInboundTextError as exc:
        logger.warning("blank whatsapp inbound %s dropped: %s", wamid, exc)
    except LookupError as exc:
        logger.warning("unrouted whatsapp inbound %s dropped: %s", wamid, exc)
    await mark_seen(wamid)
