"""Fire-and-forget notification rendering.

A notification's text, tappable ``options``/``sections``, media, and location are
rendered to their native WhatsApp shapes — reply buttons, an interactive list, a
``cta_url`` button, or a numbered-text fallback — degrading a whole message one
tier rather than truncating or shipping an over-cap value Meta would reject.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import (
    ChannelInputError,
    ChannelNotification,
    LinkOption,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.channel.interactive import (
    _CTA_URL_LABEL_MAX_CHARS,
    _FOOTER_MAX_CHARS,
    _INTERACTIVE_BODY_MAX_CHARS,
    _LIST_BUTTON_LABEL,
    _LIST_ROW_DESCRIPTION_MAX_CHARS,
    _LIST_ROW_TITLE_MAX_CHARS,
    _NUMBERED_FALLBACK_FOOTER,
    _SECTION_TITLE_MAX_CHARS,
    _interactive_choice_kind,
    _numbered_body,
)
from tai42_channel_whatsapp.channel.media import _body_with_links, _send_file_media, _send_one_file
from tai42_channel_whatsapp.client import (
    _header_object as _build_header_object,
)
from tai42_channel_whatsapp.client import (
    send_interactive_buttons,
    send_interactive_cta_url,
    send_interactive_list,
    send_location,
    send_message,
)


def _mint_wire_ids(authored: list[str | None], noun: str) -> list[str]:
    """The wire id for every tappable option on ONE message, collision-proof against authored ids.

    WhatsApp requires interactive button/list-row ids UNIQUE across
    the whole message, so a minted id that happened to equal an authored id (e.g. an
    authored ``"1"`` beside an un-id'd sibling whose 0-based index also mints ``"1"``) would
    make Meta 400 the send. Rule (mirrors the telegram channel's prefer-authored/mint-index
    discipline): use the author's id where set; else mint the option's 0-based index; if that
    minted token collides with an authored id on the SAME message (or a token already
    assigned), step deterministically (``{index}#{n}``) until it is free — the reserved set
    is finite so this terminates, and the stepped token still carries no ``:`` interaction
    part, so ``_map_tap_to_answer`` can never mistake a notification tap for a pending-ask
    reply.

    Two EQUAL authored ids on one message are an author error the wire cannot express
    (unique-id rule) — refused loudly here with ``ChannelInputError`` naming the id, before
    any send, rather than shipped for Meta to 400.
    """
    authored_ids = [value for value in authored if value is not None]
    seen: set[str] = set()
    for value in authored_ids:
        if value in seen:
            raise ChannelInputError(
                f"{noun} carries two tappable options with the same authored id {value!r}; "
                "option ids must be unique across the message"
            )
        seen.add(value)
    used = set(authored_ids)
    wire_ids: list[str] = []
    for index, value in enumerate(authored):
        if value is not None:
            wire_ids.append(value)
            continue
        candidate = str(index)
        bump = 0
        while candidate in used:
            bump += 1
            candidate = f"{index}#{bump}"
        used.add(candidate)
        wire_ids.append(candidate)
    return wire_ids


def _reply_wire_ids(replies: list[ReplyOption], wire_ids: list[str]) -> list[tuple[str, str]]:
    """``(wire_id, text)`` per reply option, aligned with the pre-minted collision-proof id list.

    ``wire_ids`` is the pre-minted list (see :func:`_mint_wire_ids`) aligned with
    ``replies``. A minted id carries no ``:`` interaction part, so ``_map_tap_to_answer``
    can never mistake a notification tap for a pending-ask reply.
    """
    return [(wire_id, reply.text) for wire_id, reply in zip(wire_ids, replies, strict=True)]


def _api_sections(sections: list[OptionSection], wire_ids: list[str]) -> list[dict[str, Any]]:
    """The Cloud-API ``action.sections`` array for a sectioned notification list.

    Each section keeps its ``title`` and maps its reply rows to
    ``{id, title, description?}``. ``wire_ids`` is the pre-minted, collision-proof id
    list (see :func:`_mint_wire_ids`) aligned with the rows read left-to-right across
    every section (list-row ids must be unique across the whole message); a present
    ``description`` rides as the row's secondary line.
    """
    api_sections: list[dict[str, Any]] = []
    cursor = 0
    for section in sections:
        rows: list[dict[str, str]] = []
        for row in section.rows:
            api_row: dict[str, str] = {"id": wire_ids[cursor], "title": row.text}
            if row.description:
                api_row["description"] = row.description
            rows.append(api_row)
            cursor += 1
        api_sections.append({"title": section.title, "rows": rows})
    return api_sections


def _sections_renderable(body: str, sections: list[OptionSection]) -> bool:
    """Whether a sectioned list fits every WhatsApp wire cap.

    An interactive body within ``_INTERACTIVE_BODY_MAX_CHARS``, each section title within
    ``_SECTION_TITLE_MAX_CHARS``, each row title within ``_LIST_ROW_TITLE_MAX_CHARS`` and
    each row description within ``_LIST_ROW_DESCRIPTION_MAX_CHARS``. A single over-cap
    field forces the whole message a tier down to numbered text (never a truncated/over-cap
    value on the wire).
    """
    if len(body) > _INTERACTIVE_BODY_MAX_CHARS:
        return False
    for section in sections:
        if len(section.title) > _SECTION_TITLE_MAX_CHARS:
            return False
        for row in section.rows:
            if len(row.text) > _LIST_ROW_TITLE_MAX_CHARS:
                return False
            if row.description is not None and len(row.description) > _LIST_ROW_DESCRIPTION_MAX_CHARS:
                return False
    return True


def _numbered_sections_body(body: str, sections: list[OptionSection], footer: str | None) -> str:
    """The numbered-text fallback for a sectioned list past the wire caps.

    The body, then each section's title as a header line with its rows numbered
    continuously (1-based across all sections), the type-an-option footer, and any
    interactive footer appended as a trailing line — the plain-text send carries no wire
    caps on these fields, so the authored titles AND row descriptions ride whole (a
    description often being the very field that forced the degrade).
    """
    lines = [body]
    index = 1
    for section in sections:
        lines.append(section.title)
        for row in section.rows:
            entry = f"{index}. {row.text} — {row.description}" if row.description else f"{index}. {row.text}"
            lines.append(entry)
            index += 1
    lines.append(_NUMBERED_FALLBACK_FOOTER)
    if footer is not None:
        lines.append(footer)
    return "\n".join(lines)


def _cta_url_renderable(body: str, link: LinkOption) -> bool:
    """Whether a lone link option fits the ``cta_url`` interactive.

    An interactive body within ``_INTERACTIVE_BODY_MAX_CHARS`` and a button
    ``display_text`` within ``_CTA_URL_LABEL_MAX_CHARS``. Otherwise the lone link
    degrades to a ``label: url`` body line (the same rendering the multi-link path uses).
    """
    return len(body) <= _INTERACTIVE_BODY_MAX_CHARS and len(link.label) <= _CTA_URL_LABEL_MAX_CHARS


def _link_option_line(option: LinkOption) -> str:
    """A :class:`LinkOption` rendered as one appended body line (``label: url``).

    The composition WhatsApp uses when a link cannot be a native button (a
    reply-buttons/list interactive rides no URL button; only the lone-link ``cta_url``
    shape carries one).
    """
    return f"{option.label}: {option.url}"


def _append_lines(body: str, lines: list[str]) -> str:
    """``body`` with each extra line appended.

    A blank ``body`` contributes no leading blank line, so the extra lines stand alone
    when the message text is empty.
    """
    if not lines:
        return body
    return "\n".join([body, *lines]) if body else "\n".join(lines)


def _folded_body(notification: ChannelNotification, links_media: list[MediaItem]) -> tuple[str, str | None]:
    """The interactive body (link media appended) and the footer.

    An over-wire-cap footer is folded into the body as a trailing line and dropped from
    the interactive footer slot — the same text-fallback idiom, leaving the body's own
    cap check to decide interactive-vs-fallback.
    """
    body = _body_with_links(notification.message, links_media)
    footer = notification.footer
    if footer is not None and len(footer) > _FOOTER_MAX_CHARS:
        body = _append_lines(body, [footer])
        footer = None
    return body, footer


def _interactive_header(header: MediaItem | None) -> dict[str, Any] | None:
    """The Cloud-API header object when a media header can ride the interactive header slot.

    Rides for image/video/document, else ``None`` (absent header, or an audio header that
    must be sent as its own message).
    """
    if header is not None and header.kind is not MediaKind.AUDIO:
        return _build_header_object(header)
    return None


async def _leading_header(
    phone_number_id: str, target: str, header: MediaItem | None, header_obj: dict[str, Any] | None
) -> list[str]:
    """Send the header as its own leading message when it is present but not on the interactive header slot.

    Applies to an audio header, or a text-fallback message; else nothing.
    """
    if header is not None and header_obj is None:
        return [await _send_one_file(phone_number_id, target, header)]
    return []


async def _render_section_list(
    phone_number_id: str,
    target: str,
    sections: list[OptionSection],
    body: str,
    header: MediaItem | None,
    footer: str | None,
) -> list[str]:
    """Render a sectioned notification as an interactive LIST, or the numbered-text fallback.

    The fallback is used when a section/row title, a row description, or the body exceeds
    its wire cap. Author-error id collisions are refused up front by
    :func:`_mint_wire_ids`, whichever tier is chosen.
    """
    section_ids = _mint_wire_ids([row.id for section in sections for row in section.rows], "notification")
    if not _sections_renderable(body, sections):
        prelude: list[str] = []
        if header is not None:
            prelude.append(await _send_one_file(phone_number_id, target, header))
        text_body = _numbered_sections_body(body, sections, footer)
        return [*prelude, await send_message(phone_number_id=phone_number_id, to=target, body=text_body)]
    header_obj = _interactive_header(header)
    prelude = await _leading_header(phone_number_id, target, header, header_obj)
    return [
        *prelude,
        await send_interactive_list(
            phone_number_id=phone_number_id,
            to=target,
            body=body,
            button_text=_LIST_BUTTON_LABEL,
            sections=_api_sections(sections, section_ids),
            header=header_obj,
            footer=footer,
        ),
    ]


async def _render_lone_cta_url(
    phone_number_id: str, target: str, link: LinkOption, body: str, header: MediaItem | None, footer: str | None
) -> list[str]:
    """Render a lone link option as a ``cta_url`` interactive (one URL button)."""
    header_obj = _interactive_header(header)
    prelude = await _leading_header(phone_number_id, target, header, header_obj)
    return [
        *prelude,
        await send_interactive_cta_url(
            phone_number_id=phone_number_id,
            to=target,
            body=body,
            display_text=link.label,
            url=link.url,
            header=header_obj,
            footer=footer,
        ),
    ]


async def _render_link_only(
    phone_number_id: str, target: str, body_with_links: str, header: MediaItem | None, footer: str | None
) -> list[str]:
    """Render an only-link notification (a lone over-cap link, or ≥2 links) as a plain text body.

    The body is ``label: url`` lines with the footer appended and any media header sent
    ahead.
    """
    prelude: list[str] = []
    if header is not None:
        prelude.append(await _send_one_file(phone_number_id, target, header))
    text_body = _append_lines(body_with_links, [footer] if footer else [])
    return [*prelude, await send_message(phone_number_id=phone_number_id, to=target, body=text_body)]


def _reply_rows(reply_ids: list[str], replies: list[ReplyOption]) -> list[dict[str, str]]:
    """The Cloud-API list rows for reply options: ``{id, title, description?}`` per reply.

    Aligned with the pre-minted collision-proof ``reply_ids``.
    """
    rows: list[dict[str, str]] = []
    for wire_id, reply in zip(reply_ids, replies, strict=True):
        row: dict[str, str] = {"id": wire_id, "title": reply.text}
        if reply.description:
            row["description"] = reply.description
        rows.append(row)
    return rows


async def _render_reply_kind(
    phone_number_id: str,
    target: str,
    kind: str,
    replies: list[ReplyOption],
    reply_ids: list[str],
    body: str,
    header_obj: dict[str, Any] | None,
    footer: str | None,
) -> str:
    """Send the reply-options message in its chosen shape (buttons / list / numbered text).

    Returns its ``wamid``. Descriptions ride the numbered lines whole on the text tier —
    a degrade never silently drops content.
    """
    if kind == "buttons":
        return await send_interactive_buttons(
            phone_number_id=phone_number_id,
            to=target,
            body=body,
            buttons=_reply_wire_ids(replies, reply_ids),
            header=header_obj,
            footer=footer,
        )
    if kind == "list":
        return await send_interactive_list(
            phone_number_id=phone_number_id,
            to=target,
            body=body,
            button_text=_LIST_BUTTON_LABEL,
            sections=[{"rows": _reply_rows(reply_ids, replies)}],
            header=header_obj,
            footer=footer,
        )
    entries = [f"{reply.text} — {reply.description}" if reply.description else reply.text for reply in replies]
    text_body = _append_lines(_numbered_body(body, entries), [footer] if footer else [])
    return await send_message(phone_number_id=phone_number_id, to=target, body=text_body)


async def _render_reply_options(
    phone_number_id: str,
    target: str,
    replies: list[ReplyOption],
    body: str,
    header: MediaItem | None,
    footer: str | None,
) -> list[str]:
    """Render reply options as buttons or a list.

    Buttons render no description, so any described row prefers the list, and an over-cap
    row title/description/body degrades the whole message to numbered text. A media header
    rides the interactive header unless the message degrades to text (or the header is
    audio), in which case it is sent ahead.
    """
    titles = [reply.text for reply in replies]
    descriptions = [reply.description for reply in replies]
    allow_buttons = not any(reply.description for reply in replies)
    kind = _interactive_choice_kind(body, titles, allow_buttons=allow_buttons, descriptions=descriptions)
    # Mint the reply ids up front (validates unique authored ids loudly even on the text tier).
    reply_ids = _mint_wire_ids([reply.id for reply in replies], "notification")
    header_obj = _interactive_header(header) if kind != "fallback" else None
    prelude = await _leading_header(phone_number_id, target, header, header_obj)
    message = await _render_reply_kind(phone_number_id, target, kind, replies, reply_ids, body, header_obj, footer)
    return [*prelude, message]


async def _render_options(
    phone_number_id: str,
    target: str,
    options: list[ReplyOption | LinkOption],
    body: str,
    header: MediaItem | None,
    footer: str | None,
) -> list[str]:
    """Render a flat ``options`` notification.

    A lone link option renders as a ``cta_url`` interactive, an only-link set as a plain
    text body, else reply options as buttons/list/numbered text. Any LINK options are
    appended to the body as ``label: url`` lines (reply widgets carry no URL button).
    """
    replies = [option for option in options if isinstance(option, ReplyOption)]
    link_options = [option for option in options if isinstance(option, LinkOption)]
    if not replies and len(link_options) == 1 and _cta_url_renderable(body, link_options[0]):
        return await _render_lone_cta_url(phone_number_id, target, link_options[0], body, header, footer)
    body_with_links = _append_lines(body, [_link_option_line(option) for option in link_options])
    if not replies:
        return await _render_link_only(phone_number_id, target, body_with_links, header, footer)
    return await _render_reply_options(phone_number_id, target, replies, body_with_links, header, footer)


async def _send_interactive_notification(
    phone_number_id: str, target: str, notification: ChannelNotification, links_media: list[MediaItem]
) -> list[str]:
    """Render an interactive notification (``options`` or ``sections``) to its native WhatsApp shape.

    Returns every ``wamid`` in send order (any audio header, or a header on a text
    fallback, rides ahead of the interactive so the actionable message stays at the
    foot). ``links_media`` are the ``link`` MEDIA items appended to the body as text
    lines.

    Wire mapping (Cloud API):

    * ``sections`` → an interactive LIST (multi-section, per-row descriptions), OR the
      numbered-text fallback when a section title / row title / row description / body exceeds
      its wire cap (the whole message degrades a tier; the authored titles ride as text lines).
    * flat ``options`` with reply entries → reply BUTTONS (≤3, unique short titles) or a LIST
      (more rows / longer titles / any row description), with any LINK options appended to the
      body as ``label: url`` lines (WhatsApp reply widgets carry no URL button); an over-cap
      row title/description/body degrades the whole message to numbered text.
    * a lone LINK option (no replies) → a ``cta_url`` interactive (one URL button), OR — when
      its ``display_text`` label exceeds the cta_url cap or the body exceeds the interactive
      body cap — the ``label: url`` body-line rendering the multi-link path uses.
    * only LINK options (≥2) → a plain text body of ``label: url`` lines (no native multi-URL
      interactive exists), the footer appended and any media header sent ahead.

    Degrade discipline for a contract-valid-but-over-wire-cap field: never truncate, never
    ship an over-cap value — degrade the WHOLE message one tier. A ``footer`` longer than the
    wire footer cap is folded into the body as a trailing line (the established text-fallback
    idiom) and the interactive footer dropped, so the body's own cap check then decides
    interactive-vs-fallback for free. Author-error id collisions are refused up front by
    :func:`_mint_wire_ids` (loud ``ChannelInputError``, before any send), regardless of the
    tier the message ends up rendering as.

    A media ``header`` (image/video/document) rides the interactive header; an ``audio``
    header or a text-fallback header is sent as its own message first. ``footer`` rides the
    interactive footer, or is appended as a body line on the text-only fallback.
    """
    body, footer = _folded_body(notification, links_media)
    header = notification.header
    if notification.sections is not None:
        return await _render_section_list(phone_number_id, target, notification.sections, body, header, footer)
    return await _render_options(phone_number_id, target, notification.options or [], body, header, footer)


async def _send_notification(phone_number_id: str, target: str, notification: ChannelNotification) -> list[str]:
    """Send a freeform notification and return every ``wamid`` in send order.

    In order: the message body / interactive choice surface (carrying the text and any
    tappable ``options``/``sections``, plus ``link`` media appended as body lines), then a
    ``location`` message, then each file-media item (image/document/video/audio) as its own
    native message. A MEDIA-ONLY / location-only notification (blank message, no options)
    skips the body send entirely. Tappable options enter the conversation as a visitor
    message on tap (their reply ids carry no pending-ask interaction part unless authored to
    collide). A file-media part that fails mid-send raises naming the wamids already delivered
    (partial delivery stays visible).
    """
    media = notification.media or []
    link_media = [item for item in media if item.kind == MediaKind.LINK]
    file_media = [item for item in media if item.kind is not MediaKind.LINK]

    sent: list[str] = []
    if notification.options is not None or notification.sections is not None:
        # An interactive surface requires a non-blank message (contract), so the body is
        # always non-blank here.
        sent = await _send_interactive_notification(phone_number_id, target, notification, link_media)
    else:
        body = _body_with_links(notification.message, link_media)
        if body:
            sent = [await send_message(phone_number_id=phone_number_id, to=target, body=body)]
        # else: a media-/location-only send — no body message, just the parts below.

    if notification.location is not None:
        location = notification.location
        sent.append(
            await send_location(
                phone_number_id=phone_number_id,
                to=target,
                latitude=location.latitude,
                longitude=location.longitude,
                name=location.name,
                address=location.address,
            )
        )
    return await _send_file_media(phone_number_id, target, file_media, sent)
