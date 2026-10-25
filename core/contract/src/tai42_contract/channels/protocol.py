"""The ``Channel`` Protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tai42_contract.channels.delivery import ChannelAddress, ChannelDelivery, SendOutcome
from tai42_contract.channels.notification import ChannelNotification


@runtime_checkable
class Channel(Protocol):
    """Delivers one question to a human on a specific medium.

    A channel plugin registers an instance under a name
    (``tai42_app.channels.register``); ``ask`` resolves it by name and calls
    ``deliver`` after the interaction is persisted and its callback ticket is
    minted. A channel never reaches the interactions store directly: the
    human's reply travels back through the delivery's public ``callback_url``.

    A channel MAY advertise richer support with seven OPTIONAL, class-level
    capability flags — ``supports_media_notifications``,
    ``supports_template_notifications``, ``supports_interactive_notifications``,
    ``supports_location_notifications``, ``supports_form_notifications`` (all
    five for ``notify``), ``supports_form_delivery`` and
    ``supports_form_reaction`` (both for ``deliver``) — set
    as plain class
    attributes. They are a documented convention, NOT Protocol members: a
    channel that supports the richer form sets the matching attribute to
    ``True``; a channel that omits it advertises no support (absent =
    ``False``). Because they are not part of the Protocol, a text-only channel
    that declares none is still a valid ``Channel`` (both structurally and under
    runtime ``isinstance``). The ask/notify helpers read them defensively with
    ``getattr(channel, "<flag>", False)`` and refuse the matching richer send to
    a channel that does not advertise the flag: ``notify_user`` refuses a media,
    template, options, sections, location or schema notification, and the
    ``ask`` helper
    refuses a ``form`` delivery, to a channel without the flag — so a channel
    that reads only the plain fields can never silently drop the extra content.
    A channel that does not advertise ``supports_form_delivery`` never receives
    a ``form`` delivery, and one that does not advertise
    ``supports_form_notifications`` never receives a ``schema`` notification. A
    channel that advertises ``supports_form_notifications`` MUST, when a form
    notification carries a ``form_tag``, carry that opaque per-send tag with the
    form on its own medium (never into the published form artefact) and hand it
    back on the participant's submission as ``accept(form_tag=...)`` — the tag
    rides the form's gate, so it needs no capability flag of its own.
    ``supports_form_reaction`` is the delivery-side flag for a REACTING form (one
    whose ask names a ``reaction_tool`` and carries ``reactions`` triggers): the
    ``ask`` helper, having already confirmed ``supports_form_delivery``, refuses a
    reacting form to a channel that does not ALSO advertise
    ``supports_form_reaction`` — so a form that reacts while open is never
    delivered to a medium that cannot route its triggers back (it is refused
    loudly at the ask door, never silently rendered inert). A non-reacting form
    needs only ``supports_form_delivery``.

    A form channel MAY also declare one OPTIONAL method, ``validate_form_schema``,
    following the same convention as the capability flags — a documented member,
    NOT a Protocol method, so declaring it never tightens the runtime structural
    check. ``ask`` reads it defensively with
    ``getattr(channel, "validate_form_schema", None)`` right after the generic
    channel-deliverable subset check and, when present, calls
    ``channel.validate_form_schema(schema, question)`` at ask-time, BEFORE any
    state is written. It enforces the channel's OWN ask-time-knowable form limits
    (reserved property names, per-medium Block Kit / Flow caps, question-text caps)
    over the schema AND the question text — limits the generic subset does not
    know — raising ``ValueError`` naming the offending property/limit on a
    violation, so a question or schema the channel could never render is refused up
    front instead of persisting a question that only fails at delivery. A channel
    that omits it advertises no extra ask-time limits; its delivery path still
    refuses an unrenderable question or schema (a permanent
    :class:`ChannelInputError`). The SAME hook is reused for a form notification:
    the notify helper calls ``channel.validate_form_schema(schema, message)`` with
    the notification's ``message`` as the ``question`` argument before the send, so
    one declared method covers both the ask and the notify form surfaces. A
    conversation's send admission runs the same hook once per form part, with the
    part's final message as the ``question``, before any part of the reply is sent.

    A channel that advertises ``supports_template_notifications`` MAY also declare one OPTIONAL
    method, ``validate_template``, following the same documented-member convention (read
    defensively with ``getattr(channel, "validate_template", None)``, NOT a Protocol method, so
    declaring it never tightens the runtime structural check). The send helpers call
    ``channel.validate_template(template)`` right after the capability gate, BEFORE the send: it
    parses the template's OPAQUE ``parameters`` against the channel's OWN template-parameter schema
    and raises ``ValueError`` on a shape the channel could never map — so a template the channel
    could never render is refused up front instead of failing at delivery. A channel that omits the
    hook advertises no ask-time parameter check; its ``notify`` still refuses an unmappable template
    at send time (a permanent :class:`ChannelInputError`). A conversation's send admission runs the
    same hook once per template part, before any part of the reply is sent. The platform never
    looks inside ``parameters`` itself; the declaring channel owns the shape end to end.

    A channel MAY also declare the OPTIONAL outbound message-length cap it splits a long answer
    against — an OPTIONAL ``ClassVar max_message_chars: int`` giving the medium's own per-message
    character limit, read defensively with ``getattr(channel, "max_message_chars", None)`` (the
    same documented-member convention, NOT a Protocol member). The conversation-delivery machine
    resolves a routed channel's split width as the operator override
    (``CONVERSATIONS_MAX_MESSAGE_CHARS``) when set, else this declared cap; a channel that declares
    neither (and has no override) is a loud config error at send, never a silent unbounded or
    truncated send.

    A channel MAY also declare an OPTIONAL ``ClassVar identity_hint: str`` — one plain sentence
    telling an operator what to enter as a conversation route's ``our_identity`` on this channel
    (which address of the medium clients message). The same documented-member convention, NOT a
    Protocol member, read with ``getattr(channel, "identity_hint", None)``. Absent means the
    channel offers no hint. A declared value must be a non-blank string; registering a channel
    that declares anything else is refused.

    A channel MAY also declare the OPTIONAL "working-on-it" (typing) signal — the
    same documented-member convention as ``validate_form_schema``
    and the capability flags, NOT Protocol members (so declaring them never tightens
    the runtime structural check and a channel that omits them stays a valid
    ``Channel``). It is two members. An OPTIONAL ``ClassVar
    working_signal_expiry_seconds: float | None`` gives the vendor indicator's lifetime
    in seconds; ``None`` (or absent) means the channel emits no working signal and the
    caller's refresh loop never starts, read defensively with ``getattr(channel,
    "working_signal_expiry_seconds", None)``. An OPTIONAL method ``async def
    signal_working(self, *, recipient: str, sender_identity: str | None = None,
    provider_message_id: str | None = None, active: bool = True) -> None`` asserts
    (``active=True``) or clears (``active=False``) the working-on-it indicator to
    ``recipient`` (the participant address, as ``notify`` addresses one), optionally
    from ``sender_identity`` (``None`` = channel default), referencing the inbound
    ``provider_message_id`` where the medium's API ties the indicator to a specific
    inbound message. A channel whose indicator self-expires implements only the assert
    and treats ``active=False`` as a no-op; a channel that emits explicit frames sends a
    start frame on ``True`` and a stop frame on ``False``. It is fire-and-forget,
    carries no reply, and raises :class:`ChannelDeliveryError` ONLY on a genuine send
    failure the caller logs, never for a transient it can ignore.

    A channel that keeps something per delivered question MAY also declare one OPTIONAL
    method, ``async def withdraw(self, withdrawal: ChannelWithdrawal) -> None`` — the same
    documented-member convention, NOT a Protocol method, read defensively with
    ``getattr(channel, "withdraw", None)``. The platform calls it when it takes a delivered
    question down by a path that is not the channel's own forwarded reply (a cancel, an erase
    of the question's thread, route or person, an expiry). The channel releases EVERYTHING it
    reserved or cached for ``withdrawal.interaction_id`` at delivery — the per-address pending
    correlation and any per-interaction sidecar — resolving the address from
    ``withdrawal.recipient`` exactly as its ``deliver`` resolved ``delivery.recipient``. It
    MUST compare the stored interaction id and never drop a hold another ask now has on the
    same address. It MUST be idempotent and a no-op when nothing is held (expired, already
    forwarded, never reserved — a link-answered format reserves nothing). It touches no
    message on the medium: the question already sent stays where it is, and a late reply to
    it takes the inbound answer ladder's no-longer-pending path. It raises
    :class:`ChannelDeliveryError` (``retryable=True`` for a transient store fault) only on a
    fault, never for "nothing to release". A channel that keeps nothing withdrawable omits the
    member (absent = nothing to release).

    A channel whose medium allows a free-form reply only within a window after the client's
    last message MAY also declare one OPTIONAL method, ``async def reply_window(self, *,
    recipient: str, sender_identity: str | None = None) -> ReplyWindow`` — the same
    documented-member convention, NOT a Protocol method, read defensively with
    ``getattr(channel, "reply_window", None)``. It reports the window for ``recipient`` (the
    client address, as ``deliver`` addresses one) and ``sender_identity`` (our identity on the
    medium; ``None`` = the channel default, resolved exactly as ``deliver`` resolves it): a
    :class:`ReplyWindow` that is ``open`` with its ``closes_at``, ``closed``, or ``none`` for a
    pair the medium keeps no window for. It reads state only and sends nothing. It raises on a
    fault (an unreachable store, a corrupt stored value, an unaddressable pair), never answers a
    guessed state. A channel that omits the member has no reply window: the platform reports
    ``none``. The medium's own rejection of a send outside its window stays the final check.
    """

    def resolve_address(self, recipient: str | None, sender_identity: str | None) -> ChannelAddress:
        """The identity and participant address this channel would send to, known before sending.

        Synchronous, no I/O. ``recipient`` and ``sender_identity`` are the send's own values
        (``None`` = the channel's default), resolved exactly as ``notify`` / ``deliver`` resolve
        them. The identity is the one the send goes from, and a completed send's
        :class:`SendOutcome` reports the same identity. The address may not be the canonical
        one: a medium that resolves a handle only when it sends returns the handle here. The
        platform uses this address only to place a send that never reported an outcome (it
        raised, it timed out, it was cancelled, or the process stopped); a completed send is
        placed by its :class:`SendOutcome`. Raises :class:`ChannelAddressError` when the target
        cannot be resolved (a recipient off the operator allowlist, no recipient and no
        configured default).
        """
        ...

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:
        """Push ``delivery`` to the medium, or raise :class:`ChannelDeliveryError`.

        Send the question to the resolved recipient — ``delivery.recipient``
        when set (after checking it against the plugin's operator allowlist),
        else the plugin's operator-configured default — and arrange for
        the reply to reach ``delivery.callback_url`` — either a tappable link
        carrying the URL, or an inbound-route correlation the plugin stores.
        Any delivery failure — an unreachable or rejecting medium, a recipient
        outside the operator allowlist, a required credential or recipient not
        configured, a bad send response — raises
        :class:`ChannelDeliveryError`; a return is the only success signal and
        yields the :class:`SendOutcome` of the question's messages (the identity
        and participant address it went from and to, and the per-message ids the
        medium assigned, empty when it exposes none). One send attempt only:
        retrying is the caller's decision, never
        an implicit loop here — the raised error's ``retryable`` and
        ``retry_after`` drive that decision, so a fault the medium can recover
        from is classified rather than blind-retried here. The dispatcher never
        hands a delivery whose ``timeout_at`` has passed.
        """
        ...

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
        """Send a fire-and-forget message, or raise :class:`ChannelDeliveryError`.

        No interaction, no ticket, no callback, no reply. Any delivery failure raises
        :class:`ChannelDeliveryError`; a permanent refusal of the input's shape or
        content (an input the medium cannot render BY NATURE) raises
        :class:`ChannelInputError` instead — retrying it cannot succeed. A return means
        the medium ACCEPTED the message —
        not that a human saw it — and yields the :class:`SendOutcome`: the identity and
        participant address the message went from and to, and the per-message ids the
        medium assigned this send (several when the medium splits a long message, empty
        when it exposes no id), which later correlate an out-of-band delivery receipt or a
        quote back to this send. One send attempt only, no retry. A channel that cannot
        notify raises :class:`NotImplementedError`.
        """
        raise NotImplementedError
