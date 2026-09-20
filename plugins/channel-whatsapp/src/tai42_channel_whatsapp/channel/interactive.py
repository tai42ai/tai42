"""Shared tappable-choice primitives and Meta interactive-message wire caps.

The select ask and the interactive notification both render options as native
reply buttons, an interactive list, or a numbered-text fallback; this module holds
the shape decision, the wire caps deciding it, and the send of one choice message.
"""

from __future__ import annotations

from tai42_channel_whatsapp.client import (
    send_interactive_buttons,
    send_interactive_list,
    send_message,
)

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


def _numbered_body(body: str, options: list[str]) -> str:
    """The numbered-text fallback body for a tappable choice past the interactive caps.

    The body, the options numbered 1-based, then the type-an-option footer.
    """
    lines = [body]
    lines.extend(f"{index}. {option}" for index, option in enumerate(options, start=1))
    lines.append(_NUMBERED_FALLBACK_FOOTER)
    return "\n".join(lines)


def _interactive_choice_kind(
    body: str,
    options: list[str],
    *,
    allow_buttons: bool = True,
    descriptions: list[str | None] | None = None,
) -> str:
    """Which native shape a tappable-choice message renders as: ``"buttons"``, ``"list"``, or ``"fallback"``.

    ``"fallback"`` is numbered text, used when it fits neither interactive shape. Shared by
    the select ask and the interactive notification.

    ``allow_buttons=False`` skips the reply-buttons shape (buttons render no per-row
    description, so a notification whose reply options carry descriptions prefers the list
    even when it would otherwise fit buttons).

    ``descriptions`` (aligned with ``options`` when given) carries each row's optional
    secondary line. A description longer than ``_LIST_ROW_DESCRIPTION_MAX_CHARS`` cannot
    ride the list either — and a described option can never be a button (its caller sets
    ``allow_buttons=False``), so the WHOLE message degrades a tier to numbered text rather
    than silently dropping authored content or shipping an over-cap value Meta 400s.
    """
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
    """Send one tappable-choice message in its native shape and return its ``wamid`` (one message).

    ``ids`` are the ``(id, title)`` reply ids; the numbered fallback carries no ids (the
    human types an option). Shared by the select ask and the interactive notification.
    """
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
