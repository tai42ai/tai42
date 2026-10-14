"""The Telegram :class:`~tai42_contract.channels.Channel`.

``deliver`` validates the bot token (``CHANNEL_TELEGRAM_BOT_TOKEN``) first, then
resolves the recipient chat: a caller-supplied ``delivery.recipient`` must be on
``CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS`` (fail closed) else the operator-set
``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT``. EVERY failure on the deliver/notify path
raises :class:`~tai42_contract.channels.ChannelDeliveryError`, operator
misconfiguration included. Each send is ONE Bot API call (the Bot API has no
idempotency key, so a failed send raises rather than risk a duplicate).

Media (:class:`MediaItem`) rides both ``deliver`` and ``notify`` as a display
enhancement. Each FILE item is sent as its own message by kind — ``image`` →
``sendPhoto``, ``document`` → ``sendDocument``, ``video`` → ``sendVideo``,
``audio`` → ``sendAudio`` (the item's caption rides along; a document's ``filename``
is not separately settable on a remote-url send, Telegram derives it from the url) —
and each ``link`` item is appended to the text body as a labelled line. A ``data:``
image is unrenderable BY NATURE (``sendPhoto`` needs a public url) and raises
:class:`~tai42_contract.channels.ChannelInputError`.

The interactive notification vocabulary maps onto native Bot API affordances:

* ``options`` — a flat list of :class:`~tai42_contract.channels.ReplyOption` /
  :class:`~tai42_contract.channels.LinkOption`. A reply option renders as an inline
  keyboard callback button (a tap submits its text); a link option renders as a native
  inline url button (a tap opens its url, no message).
* ``sections`` — Telegram has NO native sectioned list, so it degrades cleanly: each
  section's rows render as callback buttons grouped in section order, and each section
  title renders as a text header line above the keyboard.
* ``header`` — a single display-media item above an interactive message. It rides the
  standard composition: the media is sent with the body as its CAPTION and the keyboard
  attached (``sendPhoto``/``sendDocument``/``sendVideo``/``sendAudio`` carrying
  ``reply_markup``) when the body fits Telegram's caption cap; a longer body degrades to
  a separate media message followed by the text-plus-keyboard message.
* ``footer`` — a short trailing line, rendered as a muted italic line under the body
  (``parse_mode=HTML`` is used only when a footer is present, so a plain interactive send
  is unchanged).
* ``location`` — ``sendLocation`` for a bare pin, or ``sendVenue`` when BOTH a name and an
  address are present (Telegram's venue send requires a title AND an address).
* ``template`` / ``schema`` (ask-less form) notifications are NOT supported: Telegram has
  no vendor-template concept, and an ask-less form has no callback sink for a webview to
  post to, so the channel advertises neither capability flag and ``notify_user`` refuses
  the matching send up front.

A reply-option button's ``callback_data`` is the option's AUTHOR-SET id sent verbatim on
the wire when it is set and fits Telegram's 64-byte cap (the tap echoes it back); otherwise
a channel-minted token (the option's index, or a short hash when that index collides with
an author-set id on another button) — the same per-anchor side-record mapping the channel
already keeps for taps resolves either token back to the exact option. The record ALSO
keeps the author-set id and description independently of the wire token, so a bridged tap
surfaces them as ``params.reply_id`` / ``params.reply_description`` (see ``inbound``).

Tier-2 (``text``/``select``) with no inline keyboard carries
``reply_markup: {force_reply: true}`` so the reply arrives with
``reply_to_message``; the ``message_id -> callback_url`` mapping is stored before
``deliver`` returns so the inbound door can route the answer. That stored
:class:`~tai42_contract.channels.Correlation` carries the ask's ``on_mismatch`` policy
and its per-send ``wording``, so the shared answer ladder draws the caller's refusal
sentence (and honours the digression policy) when a reply lands off-format. Tier-1
(``confirm``/``external``) carries a tappable URL button to the callback door
instead — no correlation, no inbound involvement. ``form`` carries a **web_app**
button to the same callback door: it opens the schema-rendered callback page as an
in-chat webview and the page POSTs the answer straight to the door, so like Tier-1
there is no correlation and nothing arrives on the inbound route.

``notify`` is fire-and-forget. The recipient allowlist governs default/ask
sends; a bridge reply carries ``sender_identity`` (this bot's numeric id) and goes
to the initiating chat verbatim — a mismatched ``sender_identity`` is refused.

Error text never includes the request URL — the bot token is embedded in it.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import SecretStr
from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelNotification,
    Correlation,
)
from tai42_contract.interactions import CALLBACK_LINK_FORMATS
from tai42_contract.interactions.models import MediaItem
from tai42_kit.settings import require, require_secret

from tai42_channel_telegram.client import call_method, send_chat_action
from tai42_channel_telegram.compose import (
    _CAPTION_MAX_UTF16_UNITS,
    _MEDIA_SEND,
    _ask_reply_markup,
    _drawn_reply_hint,
    _file_media,
    _flat_options_keyboard,
    _guard_reply_hint,
    _interactive_body,
    _link_lines,
    _location_send,
    _media_send,
    _question_text,
    _reject_unrenderable_media,
    _sections_keyboard,
    _utf16_units,
)
from tai42_channel_telegram.correlation import (
    scoped_correlation_key,
    set_options,
    telegram_correlation_store,
)
from tai42_channel_telegram.settings import bot_numeric_id, telegram_settings


def _require_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or raise :class:`ChannelDeliveryError` naming the missing env var.

    Retypes :func:`require` for the deliver/notify path.
    """
    try:
        return require(value, "the telegram channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or raise :class:`ChannelDeliveryError` on unset/EMPTY.

    Fail CLOSED; retypes :func:`require_secret`, message names only the env var.
    """
    try:
        return require_secret(value, "the telegram channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _bot_identity(token: str) -> str:
    """This bot's numeric id, or raise :class:`ChannelDeliveryError` on a malformed token.

    Retypes :func:`bot_numeric_id` for the send path.
    """
    try:
        return bot_numeric_id(token)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _resolve_target(recipient: str | None) -> str:
    """Resolve the target chat id, fail closed against the operator allowlist.

    A caller-supplied ``recipient`` must be on
    ``CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS`` or the send is refused; ``None`` uses
    ``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT`` (must be set). The allowlist gates only
    caller-supplied values.
    """
    settings = telegram_settings()
    if recipient is None:
        return _require_delivery(settings.default_recipient, "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    if recipient not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {recipient!r} is not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS; refusing to send"
        )
    return recipient


def _result_message_id(data: dict[str, Any], context: str) -> int:
    """The integer ``result.message_id`` from an ``ok: true`` send, or raise.

    Every send returns the id the medium minted; its absence on an ``ok`` body is a
    loud :class:`ChannelDeliveryError` (a reply/tap could never be routed back to a
    send with no id).
    """
    result = data.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, int):
        raise ChannelDeliveryError(f"telegram send ok response for {context} carried no result.message_id")
    return message_id


def _result_chat_id(data: dict[str, Any], context: str) -> int:
    """The integer ``result.chat.id`` from an ``ok: true`` send, or raise.

    The AUTHORITATIVE numeric chat id Telegram resolved the delivered message to. The
    correlation and options writers scope their anchors by THIS id, never by the
    configured ``recipient`` string — a recipient may be an ``@username`` (or the numeric
    id), while the inbound reader only ever derives the numeric ``chat.id`` from an update.
    Scoping the write by the same numeric id keeps writer and reader keys aligned so an
    ``@username`` delivery's reply/tap still resolves. Its absence on an ``ok`` body is a
    loud :class:`ChannelDeliveryError` (an anchor keyed on a missing chat could never be
    read back).
    """
    result = data.get("result")
    chat = result.get("chat") if isinstance(result, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not isinstance(chat_id, int):
        raise ChannelDeliveryError(f"telegram send ok response for {context} carried no result.chat.id")
    return chat_id


class TelegramChannel:
    """Satisfies the :class:`~tai42_contract.channels.Channel` protocol.

    Stateless — settings are read at each send so a live-reload picks up rotated
    credentials with no stale per-instance snapshot.
    """

    # This channel sends file media (photo/document/video/audio) and renders tappable
    # options, sectioned lists, headers and footers as native inline keyboards; the central
    # notify_user / conversations capability guards read these before dispatching a media,
    # location or interactive notification.
    supports_media_notifications: ClassVar[bool] = True
    supports_location_notifications: ClassVar[bool] = True
    supports_interactive_notifications: ClassVar[bool] = True
    # A ``form`` ticket is delivered as a web_app button opening the
    # schema-rendered callback page in an in-chat webview (see ``deliver``).
    supports_form_delivery: ClassVar[bool] = True
    # A reacting form is honored because that server-rendered callback page posts its
    # on-change reaction round-trip to the ticket react door (the ticket in the page's
    # address IS the capability). The ask helper reads this (on top of
    # ``supports_form_delivery``) before admitting a reacting form; the webview opens the
    # same page, which routes the reaction. Absent it, a reacting form to Telegram is
    # refused loudly at the ask door.
    supports_form_reaction: ClassVar[bool] = True
    # A Bot API chat action lasts about 5 s before Telegram clears it
    # (https://core.telegram.org/bots/api#sendchataction), so the working-on-it
    # refresh loop re-sends the typing action under this lifetime.
    working_signal_expiry_seconds: ClassVar[float] = 5.0
    # Telegram caps a text message at 4096 characters; the conversation-delivery machine splits a
    # long answer against this (an operator may override it per deployment).
    max_message_chars: ClassVar[int] = 4096
    # NOTE: ``supports_template_notifications`` and ``supports_form_notifications`` are
    # deliberately ABSENT (= False): Telegram has no vendor-template concept, and an ask-less
    # form notification has no callback sink for an in-chat webview to POST to. notify_user
    # refuses the matching send up front rather than the channel silently dropping it.

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Deliver an ask ticket over Telegram: the question with its options keyboard, or a form web_app button."""
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        target = _resolve_target(delivery.recipient)

        # Refuse an unrenderable media item BEFORE any send so a multi-part delivery
        # never lands its media-or-text and then fails on an unsendable one.
        _reject_unrenderable_media(delivery.media)

        # A text ask with no options draws a force_reply whose placeholder is the caller's
        # reply hint; its 1-64 cap is guarded here, before any send, so an over-cap hint
        # lands no media and no message. A text ask WITH options draws the keyboard instead,
        # so its reply hint (if any) is not drawn and not guarded (``_drawn_reply_hint``).
        reply_hint = _drawn_reply_hint(delivery)
        if reply_hint is not None:
            _guard_reply_hint(reply_hint)

        # File media rides ahead of the question as its own message(s) (photo/document/
        # video/audio by kind); link media is appended to the question text (_question_text).
        for item in _file_media(delivery.media):
            method, payload = _media_send(target, item)
            await call_method(token, method, payload, context=f"interaction {delivery.interaction_id} media")

        payload: dict[str, Any] = {"chat_id": target, "text": _question_text(delivery)}
        payload["reply_markup"], records = _ask_reply_markup(delivery, reply_hint)

        data = await call_method(token, "sendMessage", payload, context=f"interaction {delivery.interaction_id}")

        if delivery.answer_format == "form" or delivery.answer_format in CALLBACK_LINK_FORMATS:
            return

        message_id = _result_message_id(data, f"interaction {delivery.interaction_id}")
        # The chat Telegram actually resolved the send to — the writer scopes its anchor
        # by this numeric id, matching the numeric ``chat.id`` the inbound reader derives.
        chat_id = _result_chat_id(data, f"interaction {delivery.interaction_id}")

        # Budget measured AFTER the send so the key expires at the deadline, not
        # deadline + send duration. A budget spent mid-send makes set_correlation
        # reject the non-positive TTL.
        ttl_seconds = math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds())
        entry = Correlation(
            callback_url=delivery.callback_url,
            interaction_id=delivery.interaction_id,
            ttl_deadline=delivery.timeout_at,
            # Carry the ask's digression policy and per-send wording onto the parked record so
            # the shared answer ladder reads them when a reply lands off-format: ``on_mismatch``
            # decides retry-and-notify vs bridge, and ``wording.answer.rejected`` /
            # ``rejected_final`` give the caller's refusal sentence in the end-user's language.
            on_mismatch=delivery.on_mismatch,
            wording=delivery.wording,
        )
        # The anchor is scoped by the chat it was delivered to: a Telegram message_id is
        # unique only per chat, so the reader (the inbound door) scopes by the incoming
        # update's chat to resolve exactly this ask and never another chat's same-id ask.
        # The response's ``result.chat.id`` is that chat's authoritative numeric id — an
        # ``@username`` recipient never leaks into the key, so the reader's numeric-id key
        # matches.
        try:
            await telegram_correlation_store.set_correlation(
                scoped_correlation_key(str(chat_id), str(message_id)), entry, ttl_seconds=ttl_seconds
            )
        except Exception as exc:
            raise ChannelDeliveryError(
                f"question {delivery.interaction_id} was sent (message_id={message_id}) but its "
                f"correlation could not be stored; the reply cannot be routed"
            ) from exc

        if records:
            # Keep the option records so an inbound callback_query maps its wire token back
            # to the exact option text. A store failure is loud: the keyboard's taps could
            # never be routed (a typed reply would still resolve via the correlation).
            try:
                await set_options(str(chat_id), str(message_id), records, ttl_seconds=ttl_seconds)
            except Exception as exc:
                raise ChannelDeliveryError(
                    f"question {delivery.interaction_id} was sent (message_id={message_id}) but its "
                    f"option list could not be stored; button taps cannot be routed"
                ) from exc

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send a fire-and-forget message; raise ``ChannelDeliveryError`` on any failure.

        Returns every ``message_id`` Telegram assigned, in send order.

        No reply is expected, so nothing touches the correlation store. The interactive
        surface (flat ``options`` or a sectioned list, with any ``header``/``footer``) is
        sent first, then each file media item (photo/document/video/audio) as its own
        message, then any shared ``location`` (a venue when it carries a name AND an address,
        else a bare pin). A media-only notification (blank message, no interactive surface)
        has no text body: the sendMessage is SKIPPED and only the media/location message(s)
        go out (a ``link`` item still renders as body text, so only a truly text-less send
        omits the message). A tap on a callback button enters the conversation as a visitor
        message, so the option records are kept in a side record (bounded by
        ``CHANNEL_TELEGRAM_OPTION_TAP_TTL_SECONDS``) for the inbound door to resolve. With
        ``sender_identity`` set it must equal this bot's numeric id (else a typed refusal) and
        the message goes to ``recipient`` verbatim, allowlist bypassed; unset applies the
        allowlist. Exactly one send attempt per part.
        """
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        if notification.sender_identity is not None:
            our_identity = _bot_identity(token)
            if notification.sender_identity != our_identity:
                raise ChannelDeliveryError(
                    f"sender_identity {notification.sender_identity!r} is not this bot's identity; refusing to send"
                )
            target = notification.recipient if notification.recipient is not None else _resolve_target(None)
        else:
            target = _resolve_target(notification.recipient)

        # Refuse an unrenderable media item BEFORE any send (see deliver).
        _reject_unrenderable_media(notification.media)

        sent: list[str] = []
        if notification.options is not None or notification.sections is not None:
            sent.extend(await self._send_interactive(token, target, notification))
        else:
            body = self._plain_body(notification)
            if body.strip():
                data = await call_method(
                    token, "sendMessage", {"chat_id": target, "text": body}, context="notification"
                )
                sent.append(str(_result_message_id(data, "notification")))

        for item in _file_media(notification.media):
            method, payload = _media_send(target, item)
            try:
                media_data = await call_method(token, method, payload, context="notification media")
            except ChannelDeliveryError as exc:
                raise ChannelDeliveryError(f"telegram multi-part send failed after delivering {sent}: {exc}") from exc
            sent.append(str(_result_message_id(media_data, "notification media")))

        if notification.location is not None:
            method, payload = _location_send(target, notification.location)
            try:
                location_data = await call_method(token, method, payload, context="notification location")
            except ChannelDeliveryError as exc:
                raise ChannelDeliveryError(f"telegram multi-part send failed after delivering {sent}: {exc}") from exc
            sent.append(str(_result_message_id(location_data, "notification location")))
        return sent

    async def signal_working(
        self,
        *,
        recipient: str,
        sender_identity: str | None = None,
        provider_message_id: str | None = None,
        active: bool = True,
    ) -> None:
        """Show ``recipient`` a "working on it" typing action; refreshed by the skeleton loop.

        ``active=True`` sends one ``sendChatAction`` typing to the chat; ``active=False`` is a
        documented no-op — the Bot API has no stop call and the action self-expires (~5 s). The
        chat is ``recipient`` (the numeric chat id the conversation record stores); a non-integer
        ``recipient`` is a caller error and lets ``int()`` raise. A send failure raises
        :class:`~tai42_contract.channels.ChannelDeliveryError`, which the loop logs and counts.
        """
        if not active:
            return
        await send_chat_action(int(recipient), "typing")

    @staticmethod
    def _plain_body(notification: ChannelNotification) -> str:
        """The text body of a NON-interactive notification.

        The message with any ``link`` media appended, or just the link lines for a
        media-only send (no leading blank line from an empty message).
        """
        link_lines = _link_lines(notification.media)
        if notification.message.strip():
            return "\n".join([notification.message, *link_lines])
        return "\n".join(link_lines)

    async def _send_interactive(self, token: str, target: str, notification: ChannelNotification) -> list[str]:
        """Send one interactive notification (flat ``options`` or a sectioned list) and store its option side record.

        Returns the ``message_id``(s) the send produced, in order.

        The keyboard carries the reply/link buttons; the body carries the prompt, any
        section titles, link-media lines and an italic footer. A media ``header`` rides the
        standard composition — the media sent WITH the body as caption and the keyboard
        attached when the body fits Telegram's caption cap, else a separate media message
        followed by the text-plus-keyboard message.
        """
        if notification.options is not None:
            keyboard, records = _flat_options_keyboard(notification.options)
            section_titles: list[str] = []
        else:
            if notification.sections is None:
                raise AssertionError
            keyboard, records = _sections_keyboard(notification.sections)
            section_titles = [section.title for section in notification.sections]

        body_text, parse_mode = _interactive_body(
            notification.message, _link_lines(notification.media), section_titles, notification.footer
        )

        sent: list[str] = []
        anchor = await self._send_interactive_message(
            token, target, notification.header, body_text, parse_mode, keyboard, sent
        )

        if records:
            # Keep the option records so an inbound callback_query maps its wire token back to
            # the exact option; a tap enters the conversation as a visitor message. A store
            # failure is loud: the keyboard's taps could never be routed.
            message_id = _result_message_id(anchor, "notification")
            chat_id = _result_chat_id(anchor, "notification")
            try:
                await set_options(
                    str(chat_id), str(message_id), records, ttl_seconds=telegram_settings().option_tap_ttl_seconds
                )
            except Exception as exc:
                raise ChannelDeliveryError(
                    f"notification was sent (message_id={message_id}) but its option list could not be "
                    f"stored; button taps cannot be routed"
                ) from exc
        return sent

    @staticmethod
    async def _send_interactive_message(
        token: str,
        target: str,
        header: MediaItem | None,
        body_text: str,
        parse_mode: str | None,
        keyboard: dict[str, Any],
        sent: list[str],
    ) -> dict[str, Any]:
        """Send the message that CARRIES the inline keyboard.

        Appends each send's id to ``sent`` and returns the response of the
        keyboard-carrying (anchor) message.

        With no header it is one ``sendMessage``. With a media header it rides the standard
        composition: the media sent WITH the body as its caption and the keyboard attached
        (one message) when the body fits Telegram's caption cap; a longer body degrades to a
        separate media message (no caption) followed by the text-plus-keyboard message.
        """
        if header is None:
            payload: dict[str, Any] = {"chat_id": target, "text": body_text, "reply_markup": keyboard}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            data = await call_method(token, "sendMessage", payload, context="notification")
            sent.append(str(_result_message_id(data, "notification")))
            return data

        method, source_key = _MEDIA_SEND[header.kind]
        if _utf16_units(body_text) <= _CAPTION_MAX_UTF16_UNITS:
            payload = {"chat_id": target, source_key: header.url, "caption": body_text, "reply_markup": keyboard}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            data = await call_method(token, method, payload, context="notification header")
            sent.append(str(_result_message_id(data, "notification header")))
            return data

        # Body too long for a caption: send the header media alone, then the text-plus-keyboard
        # message (which carries the keyboard and anchors the option side record).
        header_data = await call_method(
            token, method, {"chat_id": target, source_key: header.url}, context="notification header"
        )
        sent.append(str(_result_message_id(header_data, "notification header")))
        payload = {"chat_id": target, "text": body_text, "reply_markup": keyboard}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        data = await call_method(token, "sendMessage", payload, context="notification")
        sent.append(str(_result_message_id(data, "notification")))
        return data
