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

from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    NOTIFICATION_ADDRESS_MAX_CHARS,
    Channel,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
)
from tai42_contract.interactions.models import MediaItem, MediaKind
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import clamp_write_audience
from tai42_skeleton.channels.notifications_sink import record_notification
from tai42_skeleton.interactions.media import substitute_media
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore


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
    options: list[str] | None = None,
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
    ``ChannelNotification`` the channel receives; on the INTERNAL sink (``channel=None``)
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

    The notification carries no ``sender_identity`` — that field is the conversation
    bridge's — so the channel sends from its own configured identity.

    Raises ``ValueError`` for a blank message, an unknown channel name, a blank
    ``recipient``/``audience``, or a ``media``/``template``/``options`` combination the
    contract refuses (a present-but-empty list, an over-cap value, or the mutually
    exclusive media+template / options+template); ``CrossIdentityAudienceError`` when a
    restricted caller addresses another identity (a cross-identity authorization denial
    the operation door maps to a 403);
    ``NotImplementedError`` when a channel cannot notify or does not advertise the
    ``media``/``template``/``options`` capability; ``ChannelDeliveryError`` when a channel
    send fails; ``ChannelInputError`` when a channel permanently refuses the input's shape
    (an input it cannot render by nature — retrying cannot succeed) or a ``data:`` image is
    given for a channel send with no ``INTERACTIONS_PUBLIC_BASE_URL`` to mint its absolute
    url. Every failure propagates loudly — nothing is swallowed.
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-blank string")
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
    if channel is None:
        # Internal sink path. It now STORES ``media``/``template``/``options`` (parity with
        # the channel path) rather than refusing them — a clean break from the old
        # sink-carries-no-rich-content rule. The rich fields are validated exactly as the
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
        message=message, recipient=recipient, media=media, template=template, options=options
    )
    # An addressed notification lands in the identity's in-app feed even on the
    # channel path, matching ``ask_user`` (which always persists). After the clamp a
    # restricted caller always has an audience here (scoped to its own identity), so
    # its channel send records to its own feed too; only an unrestricted caller's send
    # with no audience stores nothing.
    if audience is not None:
        await record_notification(message, recipient=recipient, audience=audience)
    outbound_ids = await channel_obj.notify(notification)
    # ``None`` means accepted but no correlatable per-message id — an empty id set, not an
    # error and not a dropped id.
    if outbound_ids is None:
        return []
    return outbound_ids
