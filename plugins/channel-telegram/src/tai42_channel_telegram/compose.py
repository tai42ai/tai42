"""Compose the Bot API payloads a Telegram send puts on the wire.

Pure functions that turn a :class:`~tai42_contract.channels.ChannelDelivery` or
:class:`~tai42_contract.channels.ChannelNotification` into the ``(method, payload)``
pairs, inline keyboards, body text and option side records a send carries — no
settings read, no network call, no correlation store. :class:`TelegramChannel`
drives these from its ``deliver`` / ``notify`` paths (see :mod:`.channel`).

Media (:class:`MediaItem`) renders by kind — ``image`` → ``sendPhoto``, ``document`` →
``sendDocument``, ``video`` → ``sendVideo``, ``audio`` → ``sendAudio`` — while a ``link``
item renders as a labelled body line. The interactive vocabulary maps onto native
affordances: ``options`` / ``sections`` become inline keyboards (callback buttons for
reply options, native url buttons for link options), a ``footer`` becomes a muted italic
line (drawing the body as HTML only then), and a ``location`` becomes ``sendVenue`` (name
AND address) or ``sendLocation`` (a bare pin). An ask's ``reply_markup`` carries one
control per answer format: a form's web_app button, a Tier-1 url button, an options
keyboard, or a plain-text ask's force_reply.

A reply button's ``callback_data`` is the author-set option id sent verbatim when it is
set and fits Telegram's 64-byte cap, else a channel-minted token (the option's index, or a
short hash on collision); the side record keeps the author-set id and description so a tap
resolves back to the exact option.
"""

from __future__ import annotations

import hashlib
import html
from typing import Any

from tai42_contract.channels import (
    ChannelDelivery,
    ChannelInputError,
    LinkOption,
    Option,
    OptionSection,
)
from tai42_contract.interactions import CALLBACK_LINK_FORMATS
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind

from tai42_channel_telegram.correlation import StoredOption

# The callback-link formats (confirm/external) are answered at the callback door via a
# tappable URL button; text/select are answered through the correlation (by tapping or
# typing). ``form`` also targets the callback door but through a web_app webview button.

# Telegram's hard cap on a button's ``callback_data`` (64 bytes). An author-set option id
# is sent verbatim only when it fits; a longer id gets a minted token instead.
_CALLBACK_DATA_MAX_BYTES = 64

# Telegram's caption cap on a media message: 1024 UTF-16 code units (the Bot API counts
# UTF-16, so astral-plane characters like emoji count double). An interactive message with a
# media HEADER rides the body AS the caption when it fits; a longer body degrades to a
# separate media message followed by the text-plus-keyboard message.
_CAPTION_MAX_UTF16_UNITS = 1024

# The platform's own words for the controls a caller MAY relabel per send, made explicit as
# the default each surface shows when the caller sets no text. The form's open button reads
# ``wording.form.open``; the Tier-1 (confirm/external) open button and the text ask's reply
# input read ``wording.answer.open`` / ``wording.answer.reply_hint``.
_FORM_OPEN_LABEL = "Fill form"
_ANSWER_OPEN_LABEL = "Answer"
_REPLY_HINT = "Reply to answer"

# The deadline line drawn under EVERY question (a form and a Tier-1 ask included). The caller's
# ``wording.lines.deadline`` replaces it when set; either way the ``{deadline}`` token is filled
# by a plain substitution with Telegram's own formatted instant — the FORMAT is this channel's.
_DEADLINE_LINE = "(Answer before {deadline}.)"
_DEADLINE_FORMAT = "%Y-%m-%d %H:%M %Z"

# Telegram's documented cap on ``force_reply.input_field_placeholder``: 1-64 characters. The
# inline button texts (the form web_app button, the Tier-1 url button) have NO documented cap,
# so a Bot API refusal (ok:false) surfaces loudly through ``call_method`` rather than a guard.
_REPLY_HINT_MIN_CHARS = 1
_REPLY_HINT_MAX_CHARS = 64


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
    """The ``(method, payload)`` for a shared location.

    ``sendVenue`` when BOTH a name and an address are present (Telegram's venue send
    requires a title AND an address), else ``sendLocation`` for a bare pin (any lone
    name/address rides no native field and is dropped — Telegram exposes no venue send
    without both).
    """
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
    """A native inline keyboard for a FLAT typed-option list, and the side records for its callback buttons.

    Reply options render as callback buttons (each with a wire token and a record carrying
    its author-set id/description); link options render as native url buttons (no callback,
    no record).
    """
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
    text headers on the body (see :func:`_interactive_body`).
    """
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
    """A native inline keyboard for a select / suggested-reply ask (plain-string options), and its side records.

    The wire token is the option's index (an ask carries no author-set ids), and a tap
    submits the option's text through the correlation ladder.
    """
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
    leaving a plain interactive send byte-for-byte unchanged.
    """
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


def _deadline_line(delivery: ChannelDelivery) -> str:
    """The surfaced deadline line — the caller's ``wording.lines.deadline`` or the platform default.

    Drawn under EVERY question (a form and a Tier-1 ask included). The ``{deadline}`` token is
    filled by a plain substitution with Telegram's own formatted instant; the FORMAT is this
    channel's. ``wording.lines.answer_link`` / ``reply`` / ``options`` name no control here —
    Telegram draws a url button, a force_reply or an inline keyboard, not those sentences — so
    they are not drawn (documented).
    """
    lines = delivery.wording.lines if delivery.wording is not None else None
    template = lines.deadline if lines is not None else None
    return (template or _DEADLINE_LINE).replace("{deadline}", delivery.timeout_at.strftime(_DEADLINE_FORMAT))


def _question_text(delivery: ChannelDelivery) -> str:
    """Render the question for a plain-text chat.

    The question, any ``link`` media as labelled lines, then the surfaced deadline line
    (:func:`_deadline_line`).

    Select / suggested-reply options are NOT enumerated here — they render as the
    inline keyboard (a clean native affordance), not numbered text.
    """
    lines = [delivery.question]
    link_lines = _link_lines(delivery.media)
    if link_lines:
        lines.append("")
        lines.extend(link_lines)
    lines.append(_deadline_line(delivery))
    return "\n".join(lines)


def _drawn_reply_hint(delivery: ChannelDelivery) -> str | None:
    """The caller's reply-input hint IF this delivery draws a force_reply, else ``None``.

    The force_reply placeholder is drawn only for a question with NO inline keyboard — a text
    ask carrying no options. A form (web_app button), a Tier-1 tap (url button), or any ask
    with options (inline keyboard) draws another control, so its ``reply_hint`` names no
    control here and is not drawn: the keyboard replaces the reply prompt (documented).
    """
    if delivery.answer_format == "form" or delivery.answer_format in CALLBACK_LINK_FORMATS or delivery.options:
        return None
    answer = delivery.wording.answer if delivery.wording is not None else None
    if answer is None:
        return None
    return answer.reply_hint


def _guard_reply_hint(hint: str) -> None:
    """Refuse a reply-input hint outside Telegram's 1-64 character cap, before any send.

    ``force_reply.input_field_placeholder`` is documented 1-64 characters; an over-cap hint is
    a permanent input refusal (:class:`ChannelInputError`), never a retryable delivery failure,
    raised before any send so a refused hint lands no media and no message.
    """
    length = len(hint)
    if not (_REPLY_HINT_MIN_CHARS <= length <= _REPLY_HINT_MAX_CHARS):
        raise ChannelInputError(
            f"telegram reply hint must be {_REPLY_HINT_MIN_CHARS}-{_REPLY_HINT_MAX_CHARS} characters, got {length}"
        )


def _ask_reply_markup(delivery: ChannelDelivery, reply_hint: str | None) -> tuple[dict[str, Any], list[StoredOption]]:
    """The ``reply_markup`` for an ask send, and the option side records it anchors.

    One control per answer format, each carrying the caller's label where the control shows
    text (the platform's own word is the default):

    * ``form`` — a web_app button opening the schema-rendered callback page as an in-chat
      webview; the page POSTs the answer straight to the callback door, so there is no
      correlation and no inbound leg. web_app requires an HTTPS url — the skeleton guarantees
      the callback base is https (or localhost), so no re-validation here. Telegram accepts a
      web_app button in PRIVATE chats only, so a form to a group or channel recipient fails the
      send (ok:false) where every other format succeeds. The button text reads
      ``wording.form.open``; the form's ``cancel``/``back`` name the webview's own chrome and
      its heading (``wording.form.title``) is the callback page's — Telegram draws none of them.
    * ``confirm``/``external`` (Tier-1) — a url button opening the callback door directly (no
      correlation, no inbound). Its text reads ``wording.answer.open``; the affirmative
      (``wording.answer.confirm``) is drawn by the callback confirm PAGE, not here.
    * an ask carrying options (a ``select`` answer set, or a ``text`` ask's suggested replies)
      — a native inline keyboard, one callback button per option (a tap resolves via the
      correlation ladder; a typed reply still anchors it). The keyboard IS the choices, so
      ``wording.choice.label`` (the list-opening button on channels with a native list) draws
      nothing.
    * a plain ``text`` ask — a force_reply whose placeholder reads ``wording.answer.reply_hint``
      (guarded to Telegram's 1-64 cap by the caller before this is reached).

    Only the options case anchors side records (returned for the caller to persist); every
    other control returns an empty list.
    """
    if delivery.answer_format == "form":
        form = delivery.wording.form if delivery.wording is not None else None
        open_label = form.open if form is not None else None
        button = {"text": open_label or _FORM_OPEN_LABEL, "web_app": {"url": delivery.callback_url}}
        return {"inline_keyboard": [[button]]}, []
    if delivery.answer_format in CALLBACK_LINK_FORMATS:
        answer = delivery.wording.answer if delivery.wording is not None else None
        open_label = answer.open if answer is not None else None
        return {"inline_keyboard": [[{"text": open_label or _ANSWER_OPEN_LABEL, "url": delivery.callback_url}]]}, []
    if delivery.options:
        return _text_options_keyboard(delivery.options)
    return {"force_reply": True, "input_field_placeholder": reply_hint or _REPLY_HINT}, []
