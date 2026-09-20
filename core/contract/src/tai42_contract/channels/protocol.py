"""The ``Channel`` Protocol and the ``notify_in_order`` in-order delivery primitive."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, cast, runtime_checkable

from tai42_contract.channels.delivery import ChannelDelivery
from tai42_contract.channels.notification import ChannelNotification


@runtime_checkable
class Channel(Protocol):
    """Delivers one question to a human on a specific medium.

    A channel plugin registers an instance under a name
    (``tai42_app.channels.register``); ``ask_user`` resolves it by name and calls
    ``deliver`` after the interaction is persisted and its callback ticket is
    minted. A channel never reaches the interactions store directly: the
    human's reply travels back through the delivery's public ``callback_url``.

    A channel MAY advertise richer support with six OPTIONAL, class-level
    capability flags — ``supports_media_notifications``,
    ``supports_template_notifications``, ``supports_interactive_notifications``,
    ``supports_location_notifications``, ``supports_form_notifications`` (all
    five for ``notify``) and ``supports_form_delivery`` (for ``deliver``) — set
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
    ``ask_user`` helper
    refuses a ``form`` delivery, to a channel without the flag — so a channel
    that reads only the plain fields can never silently drop the extra content.
    A channel that does not advertise ``supports_form_delivery`` never receives
    a ``form`` delivery, and one that does not advertise
    ``supports_form_notifications`` never receives a ``schema`` notification.

    A form channel MAY also declare one OPTIONAL method, ``validate_form_schema``,
    following the same convention as the capability flags — a documented member,
    NOT a Protocol method, so declaring it never tightens the runtime structural
    check. ``ask_user`` reads it defensively with
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
    one declared method covers both the ask and the notify form surfaces.

    A channel MAY also declare one OPTIONAL method, ``deliver_ordered(notifications)``,
    for a NATIVE in-order batch (a bulk API, a transactional transcript append) — the
    same documented-member convention as ``validate_form_schema`` and the capability
    flags, NOT a Protocol method (so declaring it never tightens the runtime structural
    check and a channel that omits it stays a valid ``Channel``). It takes a
    ``Sequence[ChannelNotification]`` and returns ``list[list[str]]`` — the per-message
    ids in send order, one list per notification — sending strictly in order and never
    reordering, skipping or parallelising; the FIRST failure raises
    :class:`ChannelDeliveryError` / :class:`ChannelInputError`, with the accepted ids
    named in the exception message (as the WhatsApp body-then-media send does). A caller
    reaches the default sequential behaviour through :func:`notify_in_order`, which
    dispatches to ``deliver_ordered`` when declared and otherwise loops ``notify``; a
    channel that declares neither still delivers a batch one ``notify`` at a time.
    """

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Push ``delivery`` to the medium, or raise :class:`ChannelDeliveryError`.

        Send the question to the resolved recipient — ``delivery.recipient``
        when set (after checking it against the plugin's operator allowlist),
        else the plugin's operator-configured default — and arrange for
        the reply to reach ``delivery.callback_url`` — either a tappable link
        carrying the URL, or an inbound-route correlation the plugin stores.
        Any delivery failure — an unreachable or rejecting medium, a recipient
        outside the operator allowlist, a required credential or recipient not
        configured, a bad send response — raises
        :class:`ChannelDeliveryError`; a plain return is the only success
        signal. One send attempt only: retrying is the caller's decision, never
        an implicit loop here — the raised error's ``retryable`` and
        ``retry_after`` drive that decision, so a fault the medium can recover
        from is classified rather than blind-retried here.
        """
        ...

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send a fire-and-forget message, or raise :class:`ChannelDeliveryError`.

        No interaction, no ticket, no callback, no reply. Any delivery failure raises
        :class:`ChannelDeliveryError`; a permanent refusal of the input's shape or
        content (an input the medium cannot render BY NATURE) raises
        :class:`ChannelInputError` instead — retrying it cannot succeed. A return means
        the medium ACCEPTED the message —
        not that a human saw it — and yields the per-message ids it assigned this send
        (several when the medium splits a long message, empty when it exposes no id),
        which later correlate an out-of-band delivery receipt back to this send. One
        send attempt only, no retry. A channel that cannot notify raises
        :class:`NotImplementedError`.
        """
        raise NotImplementedError


async def notify_in_order(
    channel: Channel,
    notifications: Sequence[ChannelNotification],
    *,
    on_sent: Callable[[int, list[str]], None] | None = None,
) -> list[list[str]]:
    """Deliver ``notifications`` to ``channel`` STRICTLY in order, returning the per-message ids of each.

    One ``list[str]`` per notification, in the same order.

    The default sequential in-order primitive every channel "inherits" by a caller using
    this helper — the place a Protocol default can actually reach a structural
    implementer. When the channel declares the OPTIONAL ``deliver_ordered`` member (read
    defensively with ``getattr``, the platform's established convention for optional
    channel abilities) the batch is handed to it as a native ordered send; otherwise each
    notification is delivered with one awaited ``notify`` before the next goes out. Either
    way delivery is never reordered, never parallelised and never skipped, and it STOPS at
    the first raise (:class:`ChannelDeliveryError` / :class:`ChannelInputError`) — the
    caller learns how far the sequence got from ``on_sent`` (and, for a native batch, from
    the exception naming the accepted ids).

    ``on_sent(index, ids)`` is the progress hook a caller uses to record each accepted send
    (a send ledger, say); it fires once per notification in send order. It is intentionally
    the ONLY progress seam — a caller that needs work BETWEEN sends (a per-send lease refresh)
    drives ``notify`` itself rather than routing through this helper, which cannot express a
    pre-send hook.

    Two honesty caveats a durable caller must weigh before relying on ``on_sent``:

    - This helper is NOT yet wired into the conversations delivery machine (which chunks and
      ledgers each send inline in :mod:`tai42_skeleton.conversations.delivery`). It is the
      documented in-order primitive, not the code path a durable ordered answer currently
      flows through.
    - Per-send timing holds ONLY on the sequential (``notify``-loop) path, where ``on_sent``
      fires AFTER each accepted send and BEFORE the next goes out. On the native
      ``deliver_ordered`` path the whole batch is sent inside that one call and ``on_sent``
      fires per index only AFTER it returns — so a durable caller that needs per-send
      ledgering interleaved with the sends must NOT rely on ``on_sent`` there; it should ledger
      inline (as the conversations machine does) rather than through this helper.
    """
    ordered = list(notifications)
    native = getattr(channel, "deliver_ordered", None)
    if callable(native):
        # ``deliver_ordered`` is a documented OPTIONAL member, not a Protocol method, so it
        # is read off the instance untyped; cast it to its documented signature.
        ordered_send = cast("Callable[[Sequence[ChannelNotification]], Awaitable[list[list[str]]]]", native)
        results: list[list[str]] = await ordered_send(ordered)
        if on_sent is not None:
            for index, ids in enumerate(results):
                on_sent(index, ids)
        return results
    results = []
    for index, notification in enumerate(ordered):
        ids = await channel.notify(notification)
        results.append(ids)
        if on_sent is not None:
            on_sent(index, ids)
    return results
