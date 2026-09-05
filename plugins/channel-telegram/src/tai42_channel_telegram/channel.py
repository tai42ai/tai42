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
``deliver`` returns so the inbound door can route the answer. Tier-1
(``confirm``/``external``) carries a tappable URL button to the callback door
instead — no correlation, no inbound involvement. ``form`` carries a **web_app**
button to the same callback door: it opens the schema-rendered callback page as an
in-chat webview and the page POSTs the answer straight to the door, so like Tier-1
there is no correlation and nothing arrives on the inbound route.

``notify`` is fire-and-forget. The recipient allowlist governs default/ask_user
sends; a bridge reply carries ``sender_identity`` (this bot's numeric id) and goes
to the initiating chat verbatim — a mismatched ``sender_identity`` is refused.

Error text never includes the request URL — the bot token is embedded in it.
"""

from __future__ import annotations

import hashlib
import html
import math
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr
from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    Correlation,
    LinkOption,
    Option,
    OptionSection,
)
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind
from tai42_kit.settings import require, require_secret

from tai42_channel_telegram.client import telegram_http
from tai42_channel_telegram.correlation import (
    StoredOption,
    scoped_correlation_key,
    set_options,
    telegram_correlation_store,
)
from tai42_channel_telegram.settings import bot_numeric_id, telegram_settings

# Tier-1 (confirm/external) is answered at the callback door via a tappable URL
# button; text/select are Tier-2 (correlation, answered by tapping or typing).
# ``form`` also targets the callback door but through a web_app webview button.
_TIER1_FORMATS = frozenset({"confirm", "external"})

# Telegram's hard cap on a button's ``callback_data`` (64 bytes). An author-set option id
# is sent verbatim only when it fits; a longer id gets a minted token instead.
_CALLBACK_DATA_MAX_BYTES = 64

# Telegram's caption cap on a media message: 1024 UTF-16 code units (the Bot API counts
# UTF-16, so astral-plane characters like emoji count double). An interactive message with a
# media HEADER rides the body AS the caption when it fits; a longer body degrades to a
# separate media message followed by the text-plus-keyboard message.
_CAPTION_MAX_UTF16_UNITS = 1024


def _utf16_units(text: str) -> int:
    """The string's length in UTF-16 code units — the unit Telegram's caps count in."""
    return len(text.encode("utf-16-le")) // 2


# One display-media kind → its Bot API send method and the JSON key naming the source url.
# ``link`` is absent (it renders as an appended text line, not a file send).
_MEDIA_SEND: dict[MediaKind, tuple[str, str]] = {
    MediaKind.IMAGE: ("sendPhoto", "photo"),
    MediaKind.DOCUMENT: ("sendDocument", "document"),
    MediaKind.VIDEO: ("sendVideo", "video"),
    MediaKind.AUDIO: ("sendAudio", "audio"),
}


def _file_media(media: list[MediaItem] | None) -> list[MediaItem]:
    """The FILE items (image/document/video/audio) — each sent as its own message."""
    return [item for item in (media or []) if item.kind in _MEDIA_SEND]


def _link_line(item: MediaItem) -> str:
    """A ``link`` media item rendered as one appended text line."""
    return f"{item.caption}: {item.url}" if item.caption else item.url


def _link_lines(media: list[MediaItem] | None) -> list[str]:
    """The ``link`` items rendered as labelled body lines (appended to the text)."""
    return [_link_line(item) for item in (media or []) if item.kind is MediaKind.LINK]


def _reject_unrenderable_media(media: list[MediaItem] | None) -> None:
    """Refuse media the medium cannot render BY NATURE, before any send.

    A file send fetches a public url; an inline ``data:`` image (the only ``data:`` form
    the contract admits, image-only) has no url to fetch, so it is a permanent
    :class:`ChannelInputError` (never a retryable delivery failure) — refused up front so a
    multi-part send never lands its text or earlier items and then fails on an unsendable one.
    """
    for item in _file_media(media):
        if item.url.startswith("data:"):
            raise ChannelInputError(
                "telegram cannot send an inline data: image; sendPhoto requires a public https url "
                f"(caption={item.caption!r})"
            )


def _media_send(chat_id: str, item: MediaItem) -> tuple[str, dict[str, Any]]:
    """The ``(method, payload)`` for one file media item (caption included when set)."""
    method, source_key = _MEDIA_SEND[item.kind]
    payload: dict[str, Any] = {"chat_id": chat_id, source_key: item.url}
    if item.caption is not None:
        payload["caption"] = item.caption
    return method, payload


def _location_send(chat_id: str, location: LocationElement) -> tuple[str, dict[str, Any]]:
    """The ``(method, payload)`` for a shared location: ``sendVenue`` when BOTH a name and
    an address are present (Telegram's venue send requires a title AND an address), else
    ``sendLocation`` for a bare pin (any lone name/address rides no native field and is
    dropped — Telegram exposes no venue send without both)."""
    if location.name is not None and location.address is not None:
        return "sendVenue", {
            "chat_id": chat_id,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "title": location.name,
            "address": location.address,
        }
    return "sendLocation", {"chat_id": chat_id, "latitude": location.latitude, "longitude": location.longitude}


def _fits_callback_data(value: str) -> bool:
    """Whether ``value`` is within Telegram's 64-byte ``callback_data`` cap."""
    return len(value.encode("utf-8")) <= _CALLBACK_DATA_MAX_BYTES


def _mint_callback_data(option_id: str | None, index: int, used: set[str]) -> str:
    """The ``callback_data`` token for one reply button.

    Prefers the AUTHOR-SET ``option_id`` verbatim (the contract's echo-back semantics) when
    it is set, fits the 64-byte cap, and is not already taken by another button in this
    keyboard. Otherwise mints the option's index — the mapping the channel already uses for
    taps — and, only if that index string collides with an author-set id used elsewhere,
    falls back to a short deterministic hash so every button carries a distinct token.
    """
    if option_id is not None and _fits_callback_data(option_id) and option_id not in used:
        return option_id
    candidate = str(index)
    if candidate not in used:
        return candidate
    return f"h{hashlib.sha256(str(index).encode()).hexdigest()[:16]}"


def _reply_button(text: str, callback_data: str) -> dict[str, Any]:
    return {"text": text, "callback_data": callback_data}


def _keyboard_from_records(records: list[StoredOption], link_buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """A one-button-per-row inline keyboard from callback records then any url buttons."""
    rows = [[_reply_button(record.text, record.callback_data)] for record in records]
    rows.extend([button] for button in link_buttons)
    return {"inline_keyboard": rows}


def _flat_options_keyboard(options: list[Option]) -> tuple[dict[str, Any], list[StoredOption]]:
    """A native inline keyboard for a FLAT typed-option list, and the side records for its
    callback buttons. Reply options render as callback buttons (each with a wire token and a
    record carrying its author-set id/description); link options render as native url buttons
    (no callback, no record)."""
    records: list[StoredOption] = []
    link_buttons: list[dict[str, Any]] = []
    used: set[str] = set()
    for option in options:
        if isinstance(option, LinkOption):
            link_buttons.append({"text": option.label, "url": option.url})
            continue
        callback_data = _mint_callback_data(option.id, len(records), used)
        used.add(callback_data)
        records.append(
            StoredOption(callback_data=callback_data, text=option.text, id=option.id, description=option.description)
        )
    return _keyboard_from_records(records, link_buttons), records


def _sections_keyboard(sections: list[OptionSection]) -> tuple[dict[str, Any], list[StoredOption]]:
    """A native inline keyboard for a SECTIONED list, and the side records for its buttons.

    Telegram has no native sections, so the rows across every section render as callback
    buttons in section order (grouped by their source section); the section titles render as
    text headers on the body (see :func:`_interactive_body`)."""
    records: list[StoredOption] = []
    used: set[str] = set()
    for section in sections:
        for row in section.rows:
            callback_data = _mint_callback_data(row.id, len(records), used)
            used.add(callback_data)
            records.append(
                StoredOption(callback_data=callback_data, text=row.text, id=row.id, description=row.description)
            )
    return _keyboard_from_records(records, []), records


def _text_options_keyboard(options: list[str]) -> tuple[dict[str, Any], list[StoredOption]]:
    """A native inline keyboard for a select / suggested-reply ask (plain-string options),
    and its side records. The wire token is the option's index (an ask carries no author-set
    ids), and a tap submits the option's text through the correlation ladder."""
    records = [StoredOption(callback_data=str(index), text=text) for index, text in enumerate(options)]
    return _keyboard_from_records(records, []), records


def _interactive_body(
    message: str, link_lines: list[str], section_titles: list[str], footer: str | None
) -> tuple[str, str | None]:
    """The visible text (and ``parse_mode``) of an interactive notification.

    Assembles the prompt, any sectioned-list titles (Telegram has no native sections, so a
    title renders as a text header above its grouped keyboard rows), any link-media lines,
    and — when a footer is present — a trailing muted italic line. A footer needs inline
    formatting, so the WHOLE body is HTML-escaped and sent with ``parse_mode=HTML`` ONLY when
    a footer is present; without a footer the body is plain text and no ``parse_mode`` is set,
    leaving a plain interactive send byte-for-byte unchanged."""
    lines: list[str] = []
    if message.strip():
        lines.append(message)
    lines.extend(section_titles)
    lines.extend(link_lines)
    if footer is None:
        return "\n".join(lines), None
    escaped = [html.escape(line) for line in lines]
    escaped.append(f"<i>{html.escape(footer)}</i>")
    return "\n".join(escaped), "HTML"


def _question_text(delivery: ChannelDelivery) -> str:
    """Render the question for a plain-text chat: the question, any ``link`` media
    as labelled lines, then the surfaced deadline.

    Select / suggested-reply options are NOT enumerated here — they render as the
    inline keyboard (a clean native affordance), not numbered text.
    """
    lines = [delivery.question]
    link_lines = _link_lines(delivery.media)
    if link_lines:
        lines.append("")
        lines.extend(link_lines)
    lines.append(f"(Answer before {delivery.timeout_at.strftime('%Y-%m-%d %H:%M %Z')}.)")
    return "\n".join(lines)


def _require_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or raise :class:`ChannelDeliveryError` naming the
    missing env var (retyping :func:`require` for the deliver/notify path)."""
    try:
        return require(value, "the telegram channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or raise :class:`ChannelDeliveryError` on
    unset/EMPTY (fail CLOSED; retyping :func:`require_secret`, message names only
    the env var)."""
    try:
        return require_secret(value, "the telegram channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _bot_identity(token: str) -> str:
    """This bot's numeric id, or raise :class:`ChannelDeliveryError` on a
    malformed token (retyping :func:`bot_numeric_id` for the send path)."""
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


async def _call_bot_api(token: str, method: str, payload: dict[str, Any], context: str) -> dict[str, Any]:
    """POST ``payload`` to one Bot API ``method`` and validate the response.

    Returns the decoded ``ok: true`` body. A transport error, non-200 status,
    non-JSON body, or ``ok: false`` each raises
    :class:`~tai42_contract.channels.ChannelDeliveryError` naming ``method`` and
    ``context``. The request URL embeds the bot token and never appears in error
    text.
    """
    try:
        async with telegram_http() as client:
            response = await client.post(f"{telegram_settings().api_base_url}/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"telegram {method} failed for {context}: {type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise ChannelDeliveryError(
            f"telegram {method} returned HTTP {response.status_code} for {context}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError(f"telegram {method} returned a non-JSON body for {context}") from exc
    if not data.get("ok"):
        raise ChannelDeliveryError(
            f"telegram {method} rejected {context}: "
            f"error_code={data.get('error_code')} description={data.get('description')!r}"
        )
    return data


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
    # NOTE: ``supports_template_notifications`` and ``supports_form_notifications`` are
    # deliberately ABSENT (= False): Telegram has no vendor-template concept, and an ask-less
    # form notification has no callback sink for an in-chat webview to POST to. notify_user
    # refuses the matching send up front rather than the channel silently dropping it.

    async def deliver(self, delivery: ChannelDelivery) -> None:
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        target = _resolve_target(delivery.recipient)

        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        # Refuse an unrenderable media item BEFORE any send so a multi-part delivery
        # never lands its media-or-text and then fails on an unsendable one.
        _reject_unrenderable_media(delivery.media)

        # File media rides ahead of the question as its own message(s) (photo/document/
        # video/audio by kind); link media is appended to the question text (_question_text).
        for item in _file_media(delivery.media):
            method, payload = _media_send(target, item)
            await _call_bot_api(token, method, payload, f"interaction {delivery.interaction_id} media")

        payload: dict[str, Any] = {"chat_id": target, "text": _question_text(delivery)}
        records: list[StoredOption] = []
        if delivery.answer_format == "form":
            # A web_app button opens the schema-rendered callback page as an
            # in-chat webview; that page POSTs the answer straight to the callback
            # door, so like Tier-1 there is no correlation and no inbound leg.
            # web_app requires an HTTPS url — the skeleton guarantees the callback
            # base is https (or localhost), so no re-validation here. Telegram
            # accepts a web_app button in PRIVATE chats only, so a form to a group
            # or channel recipient fails this send (ok:false) where every other
            # answer_format succeeds.
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Fill form", "web_app": {"url": delivery.callback_url}}]]
            }
        elif delivery.answer_format in _TIER1_FORMATS:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Answer", "url": delivery.callback_url}]]}
        elif delivery.options:
            # A select ask (the answer set) or a text ask carrying suggested replies:
            # a native inline keyboard, one callback button per option. A tap resolves
            # via the correlation ladder; a manual reply to this message still anchors
            # the same correlation.
            keyboard, records = _text_options_keyboard(delivery.options)
            payload["reply_markup"] = keyboard
        else:
            payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Reply to answer"}

        data = await _call_bot_api(token, "sendMessage", payload, f"interaction {delivery.interaction_id}")

        if delivery.answer_format == "form" or delivery.answer_format in _TIER1_FORMATS:
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
        """Send a fire-and-forget message; raise ``ChannelDeliveryError`` on any
        failure. Returns every ``message_id`` Telegram assigned, in send order.

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
                data = await _call_bot_api(token, "sendMessage", {"chat_id": target, "text": body}, "notification")
                sent.append(str(_result_message_id(data, "notification")))

        for item in _file_media(notification.media):
            method, payload = _media_send(target, item)
            try:
                media_data = await _call_bot_api(token, method, payload, "notification media")
            except ChannelDeliveryError as exc:
                raise ChannelDeliveryError(f"telegram multi-part send failed after delivering {sent}: {exc}") from exc
            sent.append(str(_result_message_id(media_data, "notification media")))

        if notification.location is not None:
            method, payload = _location_send(target, notification.location)
            try:
                location_data = await _call_bot_api(token, method, payload, "notification location")
            except ChannelDeliveryError as exc:
                raise ChannelDeliveryError(f"telegram multi-part send failed after delivering {sent}: {exc}") from exc
            sent.append(str(_result_message_id(location_data, "notification location")))
        return sent

    @staticmethod
    def _plain_body(notification: ChannelNotification) -> str:
        """The text body of a NON-interactive notification: the message with any ``link``
        media appended, or just the link lines for a media-only send (no leading blank line
        from an empty message)."""
        link_lines = _link_lines(notification.media)
        if notification.message.strip():
            return "\n".join([notification.message, *link_lines])
        return "\n".join(link_lines)

    async def _send_interactive(self, token: str, target: str, notification: ChannelNotification) -> list[str]:
        """Send one interactive notification (flat ``options`` or a sectioned list) and store
        its option side record. Returns the ``message_id``(s) the send produced, in order.

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
            assert notification.sections is not None  # options XOR sections (contract)
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
        """Send the message that CARRIES the inline keyboard, appending each send's id to
        ``sent`` and returning the response of the keyboard-carrying (anchor) message.

        With no header it is one ``sendMessage``. With a media header it rides the standard
        composition: the media sent WITH the body as its caption and the keyboard attached
        (one message) when the body fits Telegram's caption cap; a longer body degrades to a
        separate media message (no caption) followed by the text-plus-keyboard message.
        """
        if header is None:
            payload: dict[str, Any] = {"chat_id": target, "text": body_text, "reply_markup": keyboard}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            data = await _call_bot_api(token, "sendMessage", payload, "notification")
            sent.append(str(_result_message_id(data, "notification")))
            return data

        method, source_key = _MEDIA_SEND[header.kind]
        if _utf16_units(body_text) <= _CAPTION_MAX_UTF16_UNITS:
            payload = {"chat_id": target, source_key: header.url, "caption": body_text, "reply_markup": keyboard}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            data = await _call_bot_api(token, method, payload, "notification header")
            sent.append(str(_result_message_id(data, "notification header")))
            return data

        # Body too long for a caption: send the header media alone, then the text-plus-keyboard
        # message (which carries the keyboard and anchors the option side record).
        header_data = await _call_bot_api(
            token, method, {"chat_id": target, source_key: header.url}, "notification header"
        )
        sent.append(str(_result_message_id(header_data, "notification header")))
        payload = {"chat_id": target, "text": body_text, "reply_markup": keyboard}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        data = await _call_bot_api(token, "sendMessage", payload, "notification")
        sent.append(str(_result_message_id(data, "notification")))
        return data
