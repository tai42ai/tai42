"""The author-facing ``notify_user`` surface — one fire-and-forget message to a
human, no reply expected.

Unlike ``ask_user`` there is no interaction, no ticket, no callback and no
blocking wait: the named channel sends the message and the call returns as soon
as the medium accepts it ("sent" means accepted by the medium, not seen by a
human). One send attempt, no retry; every failure raises loudly
(``ChannelDeliveryError`` from the channel, ``ChannelInputError`` when the channel
permanently refuses the input's shape, ``NotImplementedError`` from a
channel that cannot notify).

``channel`` is optional: with a named channel the message is delivered on that
medium; with ``channel=None`` it is recorded to the internal notifications sink
(the Studio inbox reads it back), so nothing is ever silently dropped.

``audience`` is the IDENTITY (a user_id) whose in-app inbox shows the
message — distinct from ``recipient`` (a channel delivery address). It is honored
even when a channel is set: an ``audience``-addressed call records the in-app entry
(shared + per-identity feed) REGARDLESS of whether a channel also delivers it, so
``notify_user(channel="sms", recipient=…, audience=A)`` both pushes to SMS AND lands
in A's in-app feed (matching ``ask_user``, which always persists). An UNRESTRICTED
caller's channel send with no audience stores nothing; a RESTRICTED caller's audience
is clamped to its OWN identity, so its channel send ALWAYS records to its own feed too
(in addition to the channel push) — a channel send storing nothing is the unrestricted-caller
case only.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    NOTIFICATION_ADDRESS_MAX_CHARS,
    Channel,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
    Option,
)
from tai42_contract.interactions.models import MediaItem, MediaKind
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import clamp_write_audience
from tai42_skeleton.channels.notifications_sink import record_notification
from tai42_skeleton.channels.send_receipts import index_flow_send
from tai42_skeleton.channels.send_span import active_trace_id, send_span
from tai42_skeleton.interactions.form_schema import validate_channel_form_schema
from tai42_skeleton.interactions.media import substitute_media
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore

logger = logging.getLogger(__name__)


class SenderIdentityNotAllowedError(Exception):
    """A caller supplied ``sender_identity`` on the ``notify_user`` door.

    ``sender_identity`` belongs to the conversation bridge alone; a caller-supplied value
    is rejected, never honored — it would let a caller pick which of an operator's sending
    identities a message leaves from. A domain exception, not the operations-layer
    ``BadRequestError``, so this module needs no upward dependency; the door maps it.
    """


def _resolve_channel(channel: str) -> Channel:
    """Resolve a named channel loudly — an unknown name raises ``ValueError``
    (mirroring the ``ask_user`` helper's channel guard), never a soft ignore."""
    if not isinstance(channel, str) or not channel:
        raise ValueError("channel must be a non-empty string")
    try:
        return tai42_app.channels.get(channel)
    except KeyError as exc:
        raise ValueError(f"unknown channel: {channel!r}") from exc


async def notify_user(
    message: str,
    *,
    channel: str | None = None,
    recipient: str | None = None,
    audience: str | None = None,
    media: list[MediaItem] | None = None,
    template: ChannelTemplate | None = None,
    options: list[Option] | None = None,
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Notify a human of ``message``, fire-and-forget.

    With a named ``channel`` the message is sent on that medium and the list of
    per-message ids the medium assigned the send is returned — the ids an out-of-band
    delivery receipt is later correlated back against; a return means the medium
    ACCEPTED it, not that a human saw it. One send attempt, no retry, no reply.

    With ``channel=None`` the message is recorded to the internal notifications
    sink (Redis), which the Studio inbox reads back (returning an empty id list — an
    internal record has no medium-assigned id); the interactions Redis must be
    reachable or the write raises loudly (never a silent no-op).

    ``recipient`` is an OPTIONAL per-call address (chat id, phone number, ...).
    On a named channel it is carried to the channel, which validates it against
    its operator allowlist; omitted, the channel sends to its operator-configured
    default recipient. With ``channel=None`` it is stored verbatim on the sink
    record. A set value must be a non-blank string.

    ``audience`` is the IDENTITY (a user_id) whose in-app inbox shows this,
    distinct from ``recipient``. When set, the in-app record is written (shared +
    per-identity feed) REGARDLESS of whether a channel also delivers the message —
    so an addressed notification lands in the identity's feed even on the channel
    path. A blank value is rejected.

    ``media``, ``template`` and ``options`` are OPTIONAL richer-send forms (the contract
    enforces media/template and options/template are each mutually exclusive; options may
    combine with media). On a NAMED channel they are threaded onto the
    ``ChannelNotification`` the channel receives — and an ``audience``-addressed channel
    send stores them on the in-app feed record too, so the feed shows the same rich
    content the channel delivered; on the INTERNAL sink (``channel=None``)
    they are STORED on the feed record and returned by the read doors (rendering them, media
    included, is the host inbox's own surface) — a clean break from the old
    sink-refuses-rich-content rule.
    On the channel path a channel advertises support with the OPTIONAL class attributes
    ``supports_media_notifications`` / ``supports_template_notifications`` /
    ``supports_interactive_notifications`` (absent = unsupported); the matching field sent
    to a channel that does not advertise it is a ``NotImplementedError`` (the operation
    door maps it to a 501) — a sibling channel that reads only ``notification.message`` must
    never accept the send while the extra content silently vanishes. The guard fires BEFORE
    the in-app feed record, so a refused send leaves no phantom feed entry. (The sink path
    stores rich content unconditionally — there is no channel to advertise a capability.)

    ``schema`` is the ask-less form's answer schema: the channel renders ``message`` as the
    form's prompt and ``schema`` as the fillable form, and the guest's submission enters the
    conversation as an ordinary inbound message — no ticket, no callback, no wait. It is
    CHANNEL-ONLY: a sink notification (``channel=None``) has no delivery vehicle and no
    submission door, so a schema there is refused loudly (a ``ValueError`` → 400) before any
    feed write. On a named channel it rides the ``supports_form_notifications`` capability
    flag (absent = unsupported → ``NotImplementedError`` → 501, BEFORE any feed write), then
    the shared channel-deliverable subset walk (``validate_channel_form_schema`` — the ONE
    definition the ask path uses), then the channel's OPTIONAL ``validate_form_schema``
    hook with ``message`` as the question — so a form the channel could never render is
    refused up front, never sent half-broken. The contract enforces schema/template and
    schema/options mutual exclusivity (one message, one interactive surface) and requires a
    non-blank ``message`` (a form needs a prompt); schema may combine with media. An
    ``audience``-addressed channel send stores the schema on the in-app feed record too
    (feed parity with the other rich fields).

    The notification carries no ``sender_identity`` — that field is the conversation
    bridge's — so the channel sends from its own configured identity.

    Raises ``ValueError`` for a non-string message, a blank message with no media to
    carry it (the contract admits a blank ``message`` ONLY for a media-only send), an
    unknown channel name, a blank
    ``recipient``/``audience``, a ``schema`` with ``channel=None`` or one outside the
    channel-deliverable subset (or refused by the channel's own hook), or a
    ``media``/``template``/``options``/``schema`` combination the
    contract refuses (a present-but-empty list/dict, an over-cap value, options or a schema on a
    media-only send, or the mutually
    exclusive media+template / options+template / schema+template / schema+options);
    ``CrossIdentityAudienceError`` when a
    restricted caller addresses another identity (a cross-identity authorization denial
    the operation door maps to a 403);
    ``NotImplementedError`` when a channel cannot notify or does not advertise the
    ``media``/``template``/``options``/``schema`` capability; ``ChannelDeliveryError`` when a
    channel
    send fails; ``ChannelInputError`` when a channel permanently refuses the input's shape
    (an input it cannot render by nature — retrying cannot succeed) or a ``data:`` image is
    given for a channel send with no ``INTERACTIONS_PUBLIC_BASE_URL`` to mint its absolute
    url. Every failure propagates loudly — nothing is swallowed.
    """
    # Type check only: whether a BLANK message is admissible is the contract's blank-vs-media
    # rule (blank rides only non-empty media — a caption-less, media-only send), enforced by
    # the ``ChannelNotification`` construction on both branches below, before any feed write
    # or send attempt. Duplicating the rule here would refuse media-only sends the contract
    # admits.
    if not isinstance(message, str):
        raise ValueError("message must be a string")
    if audience is not None and (not isinstance(audience, str) or not audience.strip()):
        raise ValueError("audience must be a non-empty identity")
    # ``audience`` becomes a per-identity Redis feed key and persists into the record,
    # so cap it before the clamp — symmetric with the message cap, so the two routing
    # values a caller supplies are both bounded. The clamp's own-identity fill is
    # platform data, not caller input, so it needs no cap.
    if audience is not None and len(audience) > NOTIFICATION_ADDRESS_MAX_CHARS:
        raise ValueError(f"audience must be at most {NOTIFICATION_ADDRESS_MAX_CHARS} characters, got {len(audience)}")
    # Write-side isolation clamp — before any record is written. A restricted caller
    # may address only its own feed: an unset audience is scoped to its own identity,
    # and any other identity is rejected loudly (cross-identity injection). An unrestricted
    # caller is unchanged. The cross-identity rejection is an authorization denial the
    # operation door maps to a 403 (the write-side mirror of the read door).
    audience = clamp_write_audience(audience)
    if channel is None and schema is not None:
        # A form notification NEEDS a channel: the sink has no delivery vehicle to render
        # the form and no submission door for the answers, so a feed entry carrying a
        # schema would be a form nobody can ever submit. Refused loudly (→ 400) before
        # any feed write — never stored and silently un-submittable.
        raise ValueError(
            "a form notification (schema) needs a named channel to render the form and receive the "
            "submission; the internal sink has neither"
        )
    if channel is None:
        # Internal sink path. It stores ``media``/``template``/``options`` in full parity
        # with the channel path. The rich fields are validated exactly as the
        # channel path validates them by constructing the notification the contract validates:
        # the message cap, the recipient cap, the media/options caps AND the media-vs-template
        # / options-vs-template exclusivity all raise here as a pydantic ``ValidationError`` (a
        # ``ValueError`` the operation door maps to a 400), before any feed write. Media is
        # stored RAW — a ``data:`` image renders directly under the inbox CSP — and is NOT
        # substituted to a served reference here: the shared feed key carries no TTL, so an
        # inline image can never outlive a served-media key the way a bounded interaction
        # record's substituted reference is kept alive by its group horizon.
        sink_notification = ChannelNotification(
            message=message, recipient=recipient, media=media, template=template, options=options
        )
        await record_notification(
            sink_notification.message,
            recipient=sink_notification.recipient,
            audience=audience,
            media=sink_notification.media,
            template=sink_notification.template,
            options=sink_notification.options,
        )
        return []
    channel_obj = _resolve_channel(channel)
    # Central capability guard — a channel that does not advertise the matching flag
    # receives neither field. The flags are OPTIONAL class attributes (absent = False),
    # read defensively so a text-only sibling channel is a valid target and never forced
    # to declare them. This fires BEFORE the in-app feed record below: a refused rich send
    # must leave no phantom feed entry.
    if media is not None and not getattr(channel_obj, "supports_media_notifications", False):
        raise NotImplementedError(f"channel {channel!r} does not support media notifications")
    if template is not None and not getattr(channel_obj, "supports_template_notifications", False):
        raise NotImplementedError(f"channel {channel!r} does not support template notifications")
    if options is not None and not getattr(channel_obj, "supports_interactive_notifications", False):
        raise NotImplementedError(f"channel {channel!r} does not support interactive notifications")
    if schema is not None and not getattr(channel_obj, "supports_form_notifications", False):
        raise NotImplementedError(f"channel {channel!r} does not support form notifications")
    if schema is not None:
        # The shared channel-deliverable subset walk — the ONE definition the ask path's
        # form delivery uses — refuses an unrenderable schema loudly (ValueError → 400),
        # then the channel's OPTIONAL ``validate_form_schema`` hook enforces its own
        # medium-specific limits (reserved names, per-medium caps) with the notification's
        # MESSAGE as the question, exactly as the ask path calls it. Both run BEFORE any
        # feed write or send, so a form the channel could never render leaves no trace.
        validate_channel_form_schema(schema)
        validate_form_schema = getattr(channel_obj, "validate_form_schema", None)
        if validate_form_schema is not None:
            validate_form_schema(schema, message)
    if media is not None:
        # The operation door hands this seam the request model's ``media`` as plain dicts
        # (``model_dump`` of the validated body), so coerce each to ``MediaItem`` ONCE up
        # front before any ``.kind``/``.url`` inspection — the contract shape validation
        # raising loudly on bad input (a ``ValueError`` the operation door maps to a 400).
        # The coerced items feed the data: scan, the public-base-url check and substitution.
        media = [item if isinstance(item, MediaItem) else MediaItem.model_validate(item) for item in media]
    if media is not None and any(item.kind is MediaKind.IMAGE and item.url.startswith("data:image/") for item in media):
        # A ``data:`` image cannot reach a channel as inline bytes — a vendor fetches an
        # ABSOLUTE url from its own servers. Store it by reference and swap the url for an
        # absolute served reference BEFORE the notification is built. The absolute url needs
        # the public base url; its absence is a loud channel-input refusal naming the
        # setting (the operation door maps it to a 400), never a silent drop.
        settings = interactions_settings()
        if settings.public_base_url is None:
            raise ChannelInputError(
                "a data: image on a channel notification requires INTERACTIONS_PUBLIC_BASE_URL to be set"
            )
        store = InteractionStore(settings.key_prefix)
        async with client_ctx(RedisClient, settings.redis) as r:
            media = await substitute_media(
                store, r, media, settings.idle_ttl_seconds, base_url=settings.public_base_url
            )
    # Construct (and thereby validate) the notification BEFORE the in-app feed record.
    # The contract's exclusivity validators (media+template, options+template) and the
    # present-but-empty ``media``/``options`` validators raise here as a pydantic
    # ``ValidationError`` (a ``ValueError`` the operation door maps to a 400). Running
    # them ahead of the feed write keeps the no-phantom-feed guarantee for ALL rich-send
    # refusals — the capability guards above plus these — so none leaves a stray entry.
    notification = ChannelNotification(
        message=message, recipient=recipient, media=media, template=template, options=options, schema=schema
    )
    # An addressed notification lands in the identity's in-app feed even on the
    # channel path, matching ``ask_user`` (which always persists). After the clamp a
    # restricted caller always has an audience here (scoped to its own identity), so
    # its channel send records to its own feed too; only an unrestricted caller's send
    # with no audience stores nothing. The record carries the SAME rich fields the
    # channel receives (feed parity with the sink path) — for media that is the
    # post-substitution value, so a ``data:`` image is recorded as the served absolute
    # reference the channel was handed, one value for both surfaces.
    if audience is not None:
        await record_notification(
            notification.message,
            recipient=notification.recipient,
            audience=audience,
            media=notification.media,
            template=notification.template,
            options=notification.options,
            schema=notification.schema,
        )
    # Tier 1 of the send-outcome monitoring layer: one structured ``send:<channel>`` span
    # around the single send seam. A no-op outside a flow trace; on failure the span is
    # marked ERROR with the typed ``ChannelDeliveryError``/``ChannelInputError`` detail and
    # the error re-raised unchanged (this door still raises loudly). The recipient rides the
    # span input, not metadata (see ``send_span``).
    with send_span(channel, recipient=recipient) as span:
        outbound_ids = await channel_obj.notify(notification)
        if span is not None and outbound_ids:
            # Success: stamp the accepted provider ids on the span. Gated on a live trace by
            # ``span is not None``. The tier-2 index write is deliberately kept OUT of this
            # block (see below): it is post-send monitoring bookkeeping, not part of the send.
            span.update(output={"messaging.message.id": outbound_ids})
    # Tier 2, AFTER the send span has closed SUCCESS-shaped: index each accepted id to this
    # trace/span so a later out-of-band delivery receipt for the id can be posted back onto
    # this run. The provider has already ACCEPTED the send, so this best-effort telemetry
    # write must never alter control flow or the span outcome — an interactions-Redis outage
    # here would otherwise raise a FALSE send failure (risking a double-send) and mark the
    # span ERROR with a misleading Redis error. It is caught-and-logged, never raised.
    if span is not None and outbound_ids:
        trace_id = active_trace_id()
        if trace_id is not None:
            try:
                await index_flow_send(channel, outbound_ids, trace_id=trace_id, span_id=span.id)
            except Exception:
                logger.warning(
                    "flow-send receipt indexing failed for channel %r ids %r; the send succeeded, "
                    "only later delivery-receipt correlation is lost",
                    channel,
                    outbound_ids,
                )
    # ``None`` means accepted but no correlatable per-message id — an empty id set, not an
    # error and not a dropped id.
    if outbound_ids is None:
        return []
    return outbound_ids
