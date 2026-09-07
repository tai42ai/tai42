"""The WhatsApp channel: ``deliver`` sends one question, ``notify`` sends
one fire-and-forget message.

Tier-1 formats (``confirm``, ``external``) carry the callback_url as a tappable
link and store NO correlation — the human answers via the callback door; confirm
MUST take this path (the door accepts only the link-tap bool). Tier-2 (``text``,
``select``) expect a WhatsApp reply matched through the correlation store; the
reservation is written BEFORE the send so a reply racing the send response is
still matchable, and a failed send releases the reservation and raises. A
``select`` ask renders natively where it fits — interactive reply buttons for a
few short options, an interactive list for more, and the numbered-text fallback
past those caps — and the human may always type an option instead of tapping. A
``form`` ask is also Tier-2: it renders as an in-chat WhatsApp Flow (created and
published once per answer schema, then reused), and the completed form returns as
an ``nfm_reply`` matched by the delivery's ``interaction_id`` as the flow token.
An ask-less form NOTIFICATION reuses the same Flow machinery with a namespaced
token instead of a reservation — its submission enters the conversation as a
structured visitor message, never as an answer.

Freeform sends (questions, replies, media) go to any requested recipient: Meta's
own 24-hour customer-service window is the fence (a send outside it is rejected,
error 131047, and raises). A TEMPLATE send is the one send Meta delivers cold, so
it keeps an operator fence — the recipient must be on
``CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS`` or be a known contact (a pair the inbound
webhook saw within the configured window). A recipient is always caller-supplied;
this channel has no default recipient.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4

from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
    LinkOption,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.client import (
    _header_object as _build_header_object,
)
from tai42_channel_whatsapp.client import (
    create_flow,
    delete_flow,
    publish_flow,
    send_audio,
    send_document,
    send_flow,
    send_image,
    send_interactive_buttons,
    send_interactive_cta_url,
    send_interactive_list,
    send_location,
    send_message,
    send_template,
    send_video,
)
from tai42_channel_whatsapp.correlation import (
    cache_flow_id,
    cache_flow_schema,
    get_cached_flow_id,
    is_known_contact,
    release_pending,
    reserve_pending,
)
from tai42_channel_whatsapp.flows import build_flow, build_flow_data, build_form_flow
from tai42_channel_whatsapp.settings import (
    WhatsAppSettings,
    require_delivery_setting,
    whatsapp_settings,
)

logger = logging.getLogger(__name__)

# Tier-1 answer formats resolve via the callback link, not a WhatsApp reply.
_TIER1_FORMATS = frozenset({"confirm", "external"})

# WhatsApp interactive-message caps (Meta Cloud API). A select ask (and an
# interactive notification) renders as reply buttons when its options fit the button
# caps, else as a list when every list field fits the list caps, else as numbered
# text. A field longer than its tier's wire cap forces the fallback for the WHOLE
# message — a truncated title/description would show the human content that differs
# from what the author wrote (and Meta 400s an over-cap value outright). The contract
# admits far longer strings than these wire caps (an option/section-title/footer up to
# NOTIFICATION_*_MAX_CHARS, a body up to NOTIFICATION_MESSAGE_MAX_CHARS), so a
# contract-valid notification can still exceed a wire cap; each degrade below keeps the
# whole message renderable rather than shipping a value Meta would reject.
_BUTTON_MAX_COUNT = 3  # reply-buttons: at most three buttons
_BUTTON_TITLE_MAX_CHARS = 20  # reply-button title (also must be unique)
_LIST_MAX_ROWS = 10  # list: at most ten rows across all sections
_LIST_ROW_TITLE_MAX_CHARS = 24  # list-row title
_LIST_ROW_DESCRIPTION_MAX_CHARS = 72  # list-row secondary description line
_SECTION_TITLE_MAX_CHARS = 24  # sectioned-list section header
_FOOTER_MAX_CHARS = 60  # interactive footer line
_CTA_URL_LABEL_MAX_CHARS = 20  # cta_url button display_text (label)
_INTERACTIVE_BODY_MAX_CHARS = 1024  # interactive body text
# The list-opening button label (its own 20-char cap); a fixed, generic prompt.
_LIST_BUTTON_LABEL = "Choose an option"
# The footer on the numbered-text fallback — the human types an option instead of tapping.
_NUMBERED_FALLBACK_FOOTER = "Reply with the text of one option."

# The flow-token namespace for an ASK-LESS form notification: prefix + schema hash
# + a random suffix. Inbound routing branches on this prefix BEFORE any pending-ask
# lookup, so a notify-form submission can never answer (or disturb) a question
# pending on the same pair — while an ask's flow token stays its interaction id
# verbatim and never enters this namespace. The prefix is wire-visible in every
# delivered form's token; changing it orphans the forms already sitting in chats.
_NOTIFY_FORM_TOKEN_PREFIX = "tai42-nf:"

# A Flow's name on Meta, the schema-hash suffix making it a deterministic label
# for operator legibility — NOT a uniqueness key: Meta does not enforce flow-name
# uniqueness. One Flow per distinct answer schema comes from the cache plus the
# orphan-draft cleanup, never from the name.
_FLOW_NAME_PREFIX = "tai42-form-"

# The entry screen a stepped form's send navigates to (``build_form_flow``'s first
# screen); the send injects the per-send values/options as its ``data``.
_FORM_ENTRY_SCREEN = "SCREEN_0"


async def _resolve_flow_id(waba_id: str, schema_hash: str, flow_json: dict[str, Any]) -> str:
    """The published flow id for this schema under ``waba_id``: the cached id, else
    create + publish + store a new Flow. Every step is loud — a create, publish, or
    store failure raises and never falls back to another answer format.

    A publish or cache failure AFTER a successful create strands the draft on Meta,
    and the central retry re-enters create under the same name — so the draft is
    best-effort deleted before re-raising the original error; a delete that itself
    fails is logged without masking it.
    """
    cached = await get_cached_flow_id(waba_id, schema_hash)
    if cached is not None:
        return cached
    flow_id = await create_flow(waba_id=waba_id, name=f"{_FLOW_NAME_PREFIX}{schema_hash}", flow_json=flow_json)
    try:
        await publish_flow(flow_id)
        await cache_flow_id(waba_id, schema_hash, flow_id)
    except Exception:
        try:
            await delete_flow(flow_id)
        except Exception:
            logger.exception("failed to delete orphaned draft flow %s after resolve failure", flow_id)
        raise
    return flow_id


def _form_pages_list(delivery: ChannelDelivery) -> list[dict[str, Any]] | None:
    """The form's step layout as plain JSON — each page ``{"title", "fields"}`` — or
    ``None`` when the ask carried one page."""
    if delivery.pages is None:
        return None
    return [{"title": page.title, "fields": list(page.fields)} for page in delivery.pages]


def _form_values_and_options(
    delivery: ChannelDelivery,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The form's per-send ``values`` and ``options`` as plain JSON — each option
    ``{"value", "label"?}`` (label omitted when absent). Empty when the ask carried no
    data."""
    if delivery.data is None:
        return {}, {}
    options: dict[str, list[dict[str, Any]]] = {}
    for name, choices in delivery.data.options.items():
        options[name] = [
            {"value": choice.value, **({"label": choice.label} if choice.label is not None else {})}
            for choice in choices
        ]
    return dict(delivery.data.values), options


async def _deliver_form(
    settings: WhatsAppSettings, phone_number_id: str, target: str, delivery: ChannelDelivery
) -> None:
    """Deliver a form ask as a WhatsApp Flow (Tier-2: reserve BEFORE send).

    The Flow (one screen per page) and its per-send data are built and validated
    BEFORE any network work — an unsupported schema, an unmappable per-send option, or
    an unknown page field raises here, before the ``CHANNEL_WHATSAPP_WABA_ID`` gate, the
    reservation, or a send. The reservation carries the answer schema (so an inbound
    Flow response is coerced to its types) and the question text (so a door-rejected
    answer is re-asked), and uses the ``interaction_id`` as the ``flow_token``
    correlating the completed form. The published Flow is keyed by the
    ``(schema, pages, option_fields)`` triple (the option-bearing fields decide which
    string properties render as dropdowns) and REUSED across sends; the prefilled values
    and per-send option lists ride the send's ``flow_action_payload.data`` (a dynamic
    data-source), never a new Flow.
    A failure resolving the Flow or sending releases the reservation and raises — never a
    fallback format.
    """
    if delivery.schema is None:
        raise ChannelDeliveryError(f"form delivery {delivery.interaction_id} is missing its schema")
    pages = _form_pages_list(delivery)
    values, options = _form_values_and_options(delivery)
    flow_json, schema_hash = build_form_flow(delivery.schema, pages, set(options))
    flow_data = build_flow_data(delivery.schema, values, options)
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")

    await reserve_pending(
        phone_number_id=phone_number_id,
        wa_id=target,
        callback_url=delivery.callback_url,
        timeout_at=delivery.timeout_at,
        interaction_id=delivery.interaction_id,
        schema=delivery.schema,
        question=delivery.question,
    )
    try:
        flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json)
        await send_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=delivery.question,
            flow_id=flow_id,
            flow_token=delivery.interaction_id,
            screen=_FORM_ENTRY_SCREEN,
            data=flow_data,
        )
    except Exception:
        # Any create/publish/store/send failure frees the pair instead of holding
        # it until its TTL.
        await release_pending(phone_number_id=phone_number_id, wa_id=target)
        raise


def _render_link(delivery: ChannelDelivery) -> str:
    """The message body for a Tier-1 ask (``confirm`` or ``external``): the
    question plus the tappable callback link."""
    return f"{delivery.question}\n\nAnswer here: {delivery.callback_url}"


def _numbered_body(body: str, options: list[str]) -> str:
    """The numbered-text fallback body for a tappable choice past the interactive
    caps: the body, the options numbered 1-based, then the type-an-option footer."""
    lines = [body]
    lines.extend(f"{index}. {option}" for index, option in enumerate(options, start=1))
    lines.append(_NUMBERED_FALLBACK_FOOTER)
    return "\n".join(lines)


def _interaction_ids(delivery: ChannelDelivery) -> list[tuple[str, str]]:
    """``(id, title)`` per option, id = ``{interaction_id}:{index}`` (0-based).

    The index binds the tap to the exact ask: the inbound handler requires the
    id's interaction part to equal the pending ask's before mapping the index to
    ``options[index]``.
    """
    return [(f"{delivery.interaction_id}:{index}", option) for index, option in enumerate(delivery.options or [])]


def _interactive_choice_kind(
    body: str,
    options: list[str],
    *,
    allow_buttons: bool = True,
    descriptions: list[str | None] | None = None,
) -> str:
    """Which native shape a tappable-choice message renders as: ``"buttons"``,
    ``"list"``, or ``"fallback"`` (numbered text) when it fits neither interactive
    shape. Shared by the select ask and the interactive notification.

    ``allow_buttons=False`` skips the reply-buttons shape (buttons render no per-row
    description, so a notification whose reply options carry descriptions prefers the list
    even when it would otherwise fit buttons).

    ``descriptions`` (aligned with ``options`` when given) carries each row's optional
    secondary line. A description longer than ``_LIST_ROW_DESCRIPTION_MAX_CHARS`` cannot
    ride the list either — and a described option can never be a button (its caller sets
    ``allow_buttons=False``), so the WHOLE message degrades a tier to numbered text rather
    than silently dropping authored content or shipping an over-cap value Meta 400s."""
    if len(body) > _INTERACTIVE_BODY_MAX_CHARS:
        return "fallback"
    if (
        allow_buttons
        and len(options) <= _BUTTON_MAX_COUNT
        and all(len(option) <= _BUTTON_TITLE_MAX_CHARS for option in options)
        # Reply-button titles must be unique; duplicate option text falls to a list.
        and len(set(options)) == len(options)
    ):
        return "buttons"
    if (
        len(options) <= _LIST_MAX_ROWS
        and all(len(option) <= _LIST_ROW_TITLE_MAX_CHARS for option in options)
        and (
            descriptions is None
            or all(
                description is None or len(description) <= _LIST_ROW_DESCRIPTION_MAX_CHARS
                for description in descriptions
            )
        )
    ):
        return "list"
    return "fallback"


async def _send_choice(
    phone_number_id: str, target: str, body: str, options: list[str], ids: list[tuple[str, str]]
) -> list[str]:
    """Send one tappable-choice message in its native shape and return its
    ``wamid`` (one message). ``ids`` are the ``(id, title)`` reply ids; the numbered
    fallback carries no ids (the human types an option). Shared by the select ask
    and the interactive notification."""
    kind = _interactive_choice_kind(body, options)
    if kind == "buttons":
        return [await send_interactive_buttons(phone_number_id=phone_number_id, to=target, body=body, buttons=ids)]
    if kind == "list":
        sections = [{"rows": [{"id": rid, "title": title} for rid, title in ids]}]
        return [
            await send_interactive_list(
                phone_number_id=phone_number_id, to=target, body=body, button_text=_LIST_BUTTON_LABEL, sections=sections
            )
        ]
    return [await send_message(phone_number_id=phone_number_id, to=target, body=_numbered_body(body, options))]


async def _send_question(phone_number_id: str, target: str, delivery: ChannelDelivery) -> None:
    """Push a Tier-2 question to the target in its native shape."""
    if delivery.answer_format != "select":
        await send_message(phone_number_id=phone_number_id, to=target, body=delivery.question)
        return
    await _send_choice(phone_number_id, target, delivery.question, delivery.options or [], _interaction_ids(delivery))


def _require_recipient(requested: str | None, message: str) -> str:
    """The caller-supplied recipient, or raise ``ChannelDeliveryError`` — this
    channel has no operator default recipient."""
    if requested is None:
        raise ChannelDeliveryError(message)
    return requested


_NO_DEFAULT_RECIPIENT = "no recipient requested and this channel has no default recipient; request a wa_id"


async def _resolve_template_target(settings: WhatsAppSettings, phone_number_id: str, requested: str | None) -> str:
    """The recipient for a TEMPLATE send: required, and on the allowlist OR a
    known contact of the send-from ``phone_number_id``, else refused loudly.

    A template is the one send Meta delivers cold, so it keeps an operator fence;
    the known-contact lookup keys on the resolved send-from number — a guest is
    "known" to the number they actually messaged.
    """
    target = _require_recipient(requested, _NO_DEFAULT_RECIPIENT)
    if target in set(settings.allowed_recipients):
        return target
    if await is_known_contact(phone_number_id, target):
        return target
    raise ChannelDeliveryError(
        f"template send to {target!r} refused: not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS and not a known "
        f"contact of {phone_number_id} within the configured window"
    )


def _link_line(item: MediaItem) -> str:
    """A ``link`` media item rendered as one appended body line."""
    return f"{item.caption}: {item.url}" if item.caption else item.url


def _body_with_links(message: str, links: list[MediaItem]) -> str:
    """The message body with each ``link`` media item appended as its own line. A blank
    ``message`` (a media-only send) contributes no leading blank line — the body is then the
    link lines alone, or ``""`` when there are no links (an images-only send whose body is
    skipped entirely)."""
    link_lines = [_link_line(item) for item in links]
    if not message.strip():
        return "\n".join(link_lines)
    if not link_lines:
        return message
    return "\n".join([message, *link_lines])


async def _send_one_file(phone_number_id: str, target: str, item: MediaItem) -> str:
    """Send one file-media item (image/document/video/audio) as its own native message and
    return its ``wamid``. A ``link`` never reaches here (it renders as a body line)."""
    if item.kind is MediaKind.IMAGE:
        return await send_image(phone_number_id=phone_number_id, to=target, link=item.url, caption=item.caption)
    if item.kind is MediaKind.DOCUMENT:
        return await send_document(
            phone_number_id=phone_number_id, to=target, link=item.url, caption=item.caption, filename=item.filename
        )
    if item.kind is MediaKind.VIDEO:
        return await send_video(phone_number_id=phone_number_id, to=target, link=item.url, caption=item.caption)
    # AUDIO: the Cloud API audio object carries no caption/filename (dropped at the channel).
    return await send_audio(phone_number_id=phone_number_id, to=target, link=item.url)


async def _send_file_media(phone_number_id: str, target: str, files: list[MediaItem], sent: list[str]) -> list[str]:
    """Send each file-media item (image/document/video/audio) as its own native message,
    extending ``sent`` with the minted ``wamid`` in order and returning it.

    Each item is its own message, so there is no native per-send cap here — the platform
    guard bounds the item count. A part that fails mid-send raises naming the wamids already
    delivered (partial delivery stays visible).
    """
    for item in files:
        try:
            sent.append(await _send_one_file(phone_number_id, target, item))
        except ChannelDeliveryError as exc:
            raise ChannelDeliveryError(f"WhatsApp multi-part send failed after delivering {sent}: {exc}") from exc
    return sent


async def _send_media_prelude(phone_number_id: str, target: str, media: list[MediaItem]) -> list[str]:
    """Send a delivered question's accompanying display media as its own messages,
    BEFORE the question — any ``link`` items as one text line-block, then each file item
    (image/document/video/audio) as its own native message (the same per-item send as
    ``notify``).

    Media rides ahead of the question so the actionable prompt (the last message,
    carrying any tappable widget) stays at the foot of the chat. Sent before any
    reservation, so a media failure raises with nothing reserved; a partial send
    raises naming the wamids already delivered.
    """
    files = [item for item in media if item.kind is not MediaKind.LINK]
    links = [item for item in media if item.kind == MediaKind.LINK]
    sent: list[str] = []
    if links:
        sent.append(
            await send_message(
                phone_number_id=phone_number_id, to=target, body="\n".join(_link_line(item) for item in links)
            )
        )
    return await _send_file_media(phone_number_id, target, files, sent)


def _mint_wire_ids(authored: list[str | None], noun: str) -> list[str]:
    """The wire id for every tappable option on ONE message, collision-proof against the
    AUTHORED ids present. WhatsApp requires interactive button/list-row ids UNIQUE across
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
    any send, rather than shipped for Meta to 400."""
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
    """``(wire_id, text)`` per reply option — ``wire_ids`` is the pre-minted, collision-proof
    id list (see :func:`_mint_wire_ids`) aligned with ``replies``. A minted id carries no
    ``:`` interaction part, so ``_map_tap_to_answer`` can never mistake a notification tap for
    a pending-ask reply."""
    return [(wire_id, reply.text) for wire_id, reply in zip(wire_ids, replies, strict=True)]


def _api_sections(sections: list[OptionSection], wire_ids: list[str]) -> list[dict[str, Any]]:
    """The Cloud-API ``action.sections`` array for a sectioned notification list: each
    section keeps its ``title`` and maps its reply rows to ``{id, title, description?}``.
    ``wire_ids`` is the pre-minted, collision-proof id list (see :func:`_mint_wire_ids`)
    aligned with the rows read left-to-right across every section (list-row ids must be
    unique across the whole message); a present ``description`` rides as the row's secondary
    line."""
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
    """Whether a sectioned list fits every WhatsApp wire cap: an interactive body within
    ``_INTERACTIVE_BODY_MAX_CHARS``, each section title within ``_SECTION_TITLE_MAX_CHARS``,
    each row title within ``_LIST_ROW_TITLE_MAX_CHARS`` and each row description within
    ``_LIST_ROW_DESCRIPTION_MAX_CHARS``. A single over-cap field forces the whole message a
    tier down to numbered text (never a truncated/over-cap value on the wire)."""
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
    """The numbered-text fallback for a sectioned list past the wire caps: the body, then each
    section's title as a header line with its rows numbered continuously (1-based across all
    sections), the type-an-option footer, and any interactive footer appended as a trailing
    line — the plain-text send carries no wire caps on these fields, so the authored titles
    AND row descriptions ride whole (a description often being the very field that forced
    the degrade)."""
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
    """Whether a lone link option fits the ``cta_url`` interactive: an interactive body within
    ``_INTERACTIVE_BODY_MAX_CHARS`` and a button ``display_text`` within
    ``_CTA_URL_LABEL_MAX_CHARS``. Otherwise the lone link degrades to a ``label: url`` body
    line (the same rendering the multi-link path uses)."""
    return len(body) <= _INTERACTIVE_BODY_MAX_CHARS and len(link.label) <= _CTA_URL_LABEL_MAX_CHARS


def _link_option_line(option: LinkOption) -> str:
    """A :class:`LinkOption` rendered as one appended body line (``label: url``) — the
    composition WhatsApp uses when a link cannot be a native button (a reply-buttons/list
    interactive rides no URL button; only the lone-link ``cta_url`` shape carries one)."""
    return f"{option.label}: {option.url}"


def _append_lines(body: str, lines: list[str]) -> str:
    """``body`` with each extra line appended (blank ``body`` contributes no leading blank
    line, so the extra lines stand alone when the message text is empty)."""
    if not lines:
        return body
    return "\n".join([body, *lines]) if body else "\n".join(lines)


async def _send_interactive_notification(
    phone_number_id: str, target: str, notification: ChannelNotification, links_media: list[MediaItem]
) -> list[str]:
    """Render an interactive notification (``options`` or ``sections``) to its native
    WhatsApp shape, returning every ``wamid`` in send order (any audio header, or a header on
    a text fallback, rides ahead of the interactive so the actionable message stays at the
    foot). ``links_media`` are the ``link`` MEDIA items appended to the body as text lines.

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
    body = _body_with_links(notification.message, links_media)
    header = notification.header
    footer = notification.footer
    prelude: list[str] = []

    # A footer past the wire footer cap cannot ride the interactive footer slot: fold it into
    # the body as a trailing line (the same idiom the text-only fallbacks use) and drop the
    # interactive footer. The body's own _INTERACTIVE_BODY_MAX_CHARS check below then decides
    # interactive-vs-fallback — if body+footer no longer fit, the message degrades to numbered
    # text; otherwise the interactive renders with the footer folded into its body.
    if footer is not None and len(footer) > _FOOTER_MAX_CHARS:
        body = _append_lines(body, [footer])
        footer = None

    # Sectioned list. Mint the row ids up front (validates unique authored ids loudly even when
    # the message ends up degrading to text), then render the list only if every field fits the
    # wire; otherwise degrade the whole message to numbered text.
    if notification.sections is not None:
        section_ids = _mint_wire_ids(
            [row.id for section in notification.sections for row in section.rows], "notification"
        )
        if _sections_renderable(body, notification.sections):
            header_obj = (
                _build_header_object(header) if header is not None and header.kind is not MediaKind.AUDIO else None
            )
            if header is not None and header_obj is None:
                prelude.append(await _send_one_file(phone_number_id, target, header))
            return [
                *prelude,
                await send_interactive_list(
                    phone_number_id=phone_number_id,
                    to=target,
                    body=body,
                    button_text=_LIST_BUTTON_LABEL,
                    sections=_api_sections(notification.sections, section_ids),
                    header=header_obj,
                    footer=footer,
                ),
            ]
        # Degrade: an over-cap section/row title, row description, or body forces numbered text.
        if header is not None:
            prelude.append(await _send_one_file(phone_number_id, target, header))
        text_body = _numbered_sections_body(body, notification.sections, footer)
        return [*prelude, await send_message(phone_number_id=phone_number_id, to=target, body=text_body)]

    options = notification.options or []
    replies = [option for option in options if isinstance(option, ReplyOption)]
    link_options = [option for option in options if isinstance(option, LinkOption)]

    # A lone link option → the single-URL cta_url interactive, when its label and the body fit
    # the wire; an over-cap label/body degrades it to the label: url body-line rendering below.
    if not replies and len(link_options) == 1 and _cta_url_renderable(body, link_options[0]):
        header_obj = _build_header_object(header) if header is not None and header.kind is not MediaKind.AUDIO else None
        if header is not None and header_obj is None:
            prelude.append(await _send_one_file(phone_number_id, target, header))
        return [
            *prelude,
            await send_interactive_cta_url(
                phone_number_id=phone_number_id,
                to=target,
                body=body,
                display_text=link_options[0].label,
                url=link_options[0].url,
                header=header_obj,
                footer=footer,
            ),
        ]

    body_with_links = _append_lines(body, [_link_option_line(option) for option in link_options])

    # Only link options (a lone over-cap link degraded here, or ≥2 links): no native multi-URL
    # interactive — a plain text body of the link lines, the footer appended, and any media
    # header sent ahead.
    if not replies:
        if header is not None:
            prelude.append(await _send_one_file(phone_number_id, target, header))
        text_body = _append_lines(body_with_links, [footer] if footer else [])
        return [*prelude, await send_message(phone_number_id=phone_number_id, to=target, body=text_body)]

    # Reply options → buttons or list; buttons render no description, so any described row
    # prefers the list, and an over-cap row title/description/body degrades to numbered text.
    titles = [reply.text for reply in replies]
    descriptions = [reply.description for reply in replies]
    allow_buttons = not any(reply.description for reply in replies)
    kind = _interactive_choice_kind(body_with_links, titles, allow_buttons=allow_buttons, descriptions=descriptions)
    # Mint the reply ids up front (validates unique authored ids loudly even on the text tier).
    reply_ids = _mint_wire_ids([reply.id for reply in replies], "notification")

    header_rides = header is not None and kind != "fallback" and header.kind is not MediaKind.AUDIO
    header_obj = _build_header_object(header) if header is not None and header_rides else None
    if header is not None and not header_rides:
        prelude.append(await _send_one_file(phone_number_id, target, header))

    if kind == "buttons":
        return [
            *prelude,
            await send_interactive_buttons(
                phone_number_id=phone_number_id,
                to=target,
                body=body_with_links,
                buttons=_reply_wire_ids(replies, reply_ids),
                header=header_obj,
                footer=footer,
            ),
        ]
    if kind == "list":
        rows: list[dict[str, str]] = []
        for wire_id, reply in zip(reply_ids, replies, strict=True):
            row: dict[str, str] = {"id": wire_id, "title": reply.text}
            if reply.description:
                row["description"] = reply.description
            rows.append(row)
        return [
            *prelude,
            await send_interactive_list(
                phone_number_id=phone_number_id,
                to=target,
                body=body_with_links,
                button_text=_LIST_BUTTON_LABEL,
                sections=[{"rows": rows}],
                header=header_obj,
                footer=footer,
            ),
        ]
    # Numbered-text fallback: the person types an option (which bridges as a visitor message).
    # Descriptions ride the numbered lines whole — a degrade never silently drops content.
    entries = [f"{reply.text} — {reply.description}" if reply.description else reply.text for reply in replies]
    numbered = _numbered_body(body_with_links, entries)
    text_body = _append_lines(numbered, [footer] if footer else [])
    return [*prelude, await send_message(phone_number_id=phone_number_id, to=target, body=text_body)]


async def _send_notification(phone_number_id: str, target: str, notification: ChannelNotification) -> list[str]:
    """Send a freeform notification and return every ``wamid`` in send order.

    Order: the message body / interactive choice surface (carrying the text and any
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


async def _send_form_notification(
    settings: WhatsAppSettings, phone_number_id: str, target: str, notification: ChannelNotification
) -> list[str]:
    """Send an ask-less form notification: any display media as the standard prelude
    (the ``link`` items as one text line-block, each ``image`` as its own message),
    then the Flow message LAST — the actionable prompt stays at the foot of the chat.
    Returns every ``wamid`` in send order.

    The Flow is resolved exactly like a form ask's (one published Flow per answer
    schema, cached under the WABA id), and the answer schema itself is cached beside
    the flow id — the submission's reply carries only the schema hash inside its
    flow token, so that sidecar is the ONLY place the inbound side can recover the
    schema to coerce the values (see :func:`cache_flow_schema`). NO correlation is
    reserved: the token — minted in the ``tai42-nf:`` namespace as prefix + schema
    hash + a random suffix — routes the reply, not the pair, so any number of forms
    may be outstanding and a pending ask on the same pair is never touched.
    """
    if notification.schema is None:  # dispatch guard; notify() branches on the field
        raise ChannelDeliveryError("form notification is missing its schema")
    flow_json, schema_hash = build_flow(notification.schema)
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")

    sent = await _send_media_prelude(phone_number_id, target, list(notification.media or []))
    flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json)
    # Written beside every flow-id use — the reply side cannot repopulate it.
    await cache_flow_schema(waba_id, schema_hash, notification.schema)
    flow_token = f"{_NOTIFY_FORM_TOKEN_PREFIX}{schema_hash}:{uuid4().hex}"
    sent.append(
        await send_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=notification.message,
            flow_id=flow_id,
            flow_token=flow_token,
        )
    )
    return sent


async def _send_template(
    settings: WhatsAppSettings, phone_number_id: str, target: str, template: ChannelTemplate
) -> list[str]:
    """Enforce the template recipient policy, then send the template."""
    resolved = await _resolve_template_target(settings, phone_number_id, target)
    return [await send_template(phone_number_id=phone_number_id, to=resolved, template=template)]


class WhatsAppChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol."""

    # This channel sends media (image/document/video/audio + link) and out-of-window
    # templates, shares a geographic location, renders a notification's tappable
    # options/sections as native reply buttons/list/cta_url (with a media header + footer),
    # and renders an ask-less form notification as a WhatsApp Flow; the central notify_user
    # (and conversation-delivery) capability guard reads these before dispatching each.
    supports_media_notifications: ClassVar[bool] = True
    supports_location_notifications: ClassVar[bool] = True
    supports_template_notifications: ClassVar[bool] = True
    supports_interactive_notifications: ClassVar[bool] = True
    supports_form_notifications: ClassVar[bool] = True
    # This channel renders a form ask as a WhatsApp Flow; the ask_user helper reads
    # this before handing a form delivery over.
    supports_form_delivery: ClassVar[bool] = True

    def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
        """Enforce this channel's form-schema limits at ask-time, before any state
        is written. The reserved ``flow_token`` property (Meta's own correlation
        key) — and every subset rule the Flow mapping enforces — is refused here as
        a ``ValueError``, so a schema the delivery path could never render is
        rejected up front instead of persisting a question that only fails at
        delivery. ``build_flow`` is the single mapping definition; a delivery-time
        ``ChannelInputError`` becomes the ask-time ``ValueError``. The Flow body is
        ``interactive.body.text``, capped by Meta at ``_INTERACTIVE_BODY_MAX_CHARS``,
        so an over-long ``question`` is refused here too."""
        if len(question) > _INTERACTIVE_BODY_MAX_CHARS:
            raise ValueError(f"form question exceeds {_INTERACTIVE_BODY_MAX_CHARS} characters")
        try:
            build_flow(schema)
        except ChannelInputError as exc:
            raise ValueError(str(exc)) from exc

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Resolve the destination ``wa_id``, then push the question to it.

        The question is a freeform send, so the recipient is required but not
        allowlist-fenced (Meta's 24-hour window is the fence). An ask already past
        its deadline is refused loudly for every format before any reservation or
        send. Any accompanying display ``media`` is sent FIRST — each ``image`` as
        its own image message and any ``link`` items as a text line-block, the same
        per-item send ``notify`` uses — so the question (carrying any tappable
        widget) stays the last, actionable message; a media failure raises before
        any reservation. A select ask renders in its native shape (buttons/list/
        numbered text); the reservation carries the ask's options and interaction id
        so an interactive tap resolves back to the exact option text.
        """
        settings = whatsapp_settings()
        phone_number_id = require_delivery_setting(
            settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
        )
        target = _require_recipient(delivery.recipient, _NO_DEFAULT_RECIPIENT)

        # Refuse a question whose answer budget is already spent, before any
        # reservation or HTTP work.
        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        # Display media rides ahead of the question — sent before any reservation, so
        # a media failure raises with nothing reserved.
        if delivery.media:
            await _send_media_prelude(phone_number_id, target, list(delivery.media))

        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1: answered via the callback link, so no correlation is stored.
            await send_message(phone_number_id=phone_number_id, to=target, body=_render_link(delivery))
            return

        if delivery.answer_format == "form":
            await _deliver_form(settings, phone_number_id, target, delivery)
            return

        # Reserve before send: a fast reply's webhook can beat the send response,
        # and this enforces one-pending-per-pair before any network cost.
        await reserve_pending(
            phone_number_id=phone_number_id,
            wa_id=target,
            callback_url=delivery.callback_url,
            timeout_at=delivery.timeout_at,
            options=delivery.options,
            interaction_id=delivery.interaction_id,
        )
        try:
            await _send_question(phone_number_id, target, delivery)
        except Exception:
            # Send failed — free the pair instead of holding it until its TTL.
            await release_pending(phone_number_id=phone_number_id, wa_id=target)
            raise

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any
        failure. Returns every ``wamid`` the Cloud API assigned, in send order.

        No reply is expected, so nothing touches the correlation store. Exactly one
        send attempt per part (a plain return means Meta ACCEPTED it, not that a
        human saw it). ``sender_identity`` set → send FROM that ``phone_number_id``;
        unset → the configured ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID``. When
        ``template`` is set the send is the out-of-window template (recipient
        allowlist-or-known-contact); when ``schema`` is set the send is an ask-less
        FORM — any media as the prelude, then a WhatsApp Flow whose body is the
        message and whose flow token rides the ``tai42-nf:`` namespace (a submission
        enters the conversation as a structured visitor message; nothing is
        reserved); otherwise the freeform body plus any tappable ``options`` (native
        reply buttons/list — a tap enters the conversation as a visitor message) and
        any media parts (freeform recipient unfenced). The recipient is always
        required.
        """
        settings = whatsapp_settings()
        if notification.sender_identity is not None:
            phone_number_id = notification.sender_identity
            target = _require_recipient(
                notification.recipient, "a bridge reply requires a recipient wa_id; none was provided"
            )
        else:
            phone_number_id = require_delivery_setting(
                settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
            )
            target = _require_recipient(notification.recipient, _NO_DEFAULT_RECIPIENT)

        if notification.template is not None:
            return await _send_template(settings, phone_number_id, target, notification.template)
        if notification.schema is not None:
            return await _send_form_notification(settings, phone_number_id, target, notification)
        return await _send_notification(phone_number_id, target, notification)
