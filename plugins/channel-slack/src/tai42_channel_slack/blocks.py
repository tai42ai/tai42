"""Block Kit builders for the non-form richer sends: display media and tappable
option buttons.

Both ``deliver`` and ``notify`` reuse these. ``media`` (:class:`MediaItem`) renders
as native Block Kit: an ``image`` item as an ``image`` block (a public ``https``
url — a ``data:`` image is unrenderable BY NATURE and raises
:class:`~tai42_contract.channels.ChannelInputError`), a ``link`` item as a
``section`` with an mrkdwn link. ``options`` render as an ``actions`` block of
buttons (one per option) when they fit Slack's caps — at most
:data:`_MAX_OPTION_BUTTONS` buttons, each label at most
:data:`_MAX_BUTTON_TEXT_LEN` characters; a button's ``value`` is the option text
verbatim and its ``action_id`` carries the option index, so an inbound tap maps back
to the exact option. Past those caps the buttons are omitted (the caller keeps the
options as text — a select ask's numbered fallback, a notify's appended suggestion
lines), never a silently truncated label that would submit a different string.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions.models import MediaItem, MediaKind

# Each option button's action_id is ``tai42_select:<index>`` — the prefix marks it a
# select/suggested-reply tap on the interactivity door, the index binds it to the
# option in send order.
SELECT_ACTION_PREFIX = "tai42_select:"

# Slack Block Kit caps for an actions block of buttons.
_MAX_OPTION_BUTTONS = 25  # elements per actions block
_MAX_BUTTON_TEXT_LEN = 75  # a button's plain_text label
# A button's ``value`` cap (Slack allows 2000); every option comfortably fits, but the
# guard keeps a pathological option from minting a value Slack rejects at send.
_MAX_BUTTON_VALUE_LEN = 2000
# ``alt_text`` is accessibility text (not the media content), so it is bounded, never
# a content field — a caption longer than this is clipped for the alt attribute only.
_MAX_ALT_TEXT_LEN = 2000


def is_select_action(action_id: str) -> bool:
    """Whether ``action_id`` is one of this channel's option-button taps."""
    return action_id.startswith(SELECT_ACTION_PREFIX)


def _link_mrkdwn(item: MediaItem) -> str:
    """A ``link`` media item as an mrkdwn hyperlink (``<url|caption>``)."""
    return f"<{item.url}|{item.caption}>" if item.caption else f"<{item.url}>"


def build_media_blocks(media: list[MediaItem] | None) -> list[dict[str, Any]]:
    """The display-media blocks for a message: each ``image`` item an image block,
    each ``link`` item an mrkdwn section.

    A ``data:`` image has no public url for Slack to fetch, so it is a permanent
    :class:`ChannelInputError` (never a retryable delivery failure) — refused here,
    before any send, so a richer message never posts its text and then fails on an
    unrenderable image.
    """
    blocks: list[dict[str, Any]] = []
    for item in media or []:
        if item.kind is MediaKind.IMAGE:
            if item.url.startswith("data:"):
                raise ChannelInputError(
                    "slack cannot render an inline data: image; an image block requires a public https url "
                    f"(caption={item.caption!r})"
                )
            block: dict[str, Any] = {"type": "image", "image_url": item.url}
            # alt_text is required and is accessibility text only — safe to bound.
            block["alt_text"] = (item.caption or "image")[:_MAX_ALT_TEXT_LEN]
            blocks.append(block)
        else:  # MediaKind.LINK
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _link_mrkdwn(item)}})
    return blocks


def options_fit_buttons(options: list[str]) -> bool:
    """Whether ``options`` render as a native actions block within Slack's caps
    (button count and per-label length). Past them the caller keeps them as text —
    never a truncated label that would submit a value different from what is shown.
    """
    return len(options) <= _MAX_OPTION_BUTTONS and all(
        len(option) <= _MAX_BUTTON_TEXT_LEN and len(option) <= _MAX_BUTTON_VALUE_LEN for option in options
    )


def build_option_blocks(options: list[str] | None) -> list[dict[str, Any]]:
    """An ``actions`` block of option buttons when the options fit Slack's caps, else
    an empty list (no options, or past the caps — the caller renders them as text).

    Each button's ``value`` is the option text verbatim (mapped straight back on a
    tap) and its ``action_id`` is ``tai42_select:<index>`` (the interactivity door
    reads the prefix to route the tap and the index to bind it to the ask).
    """
    if not options or not options_fit_buttons(options):
        return []
    elements = [
        {
            "type": "button",
            "action_id": f"{SELECT_ACTION_PREFIX}{index}",
            "text": {"type": "plain_text", "text": option},
            "value": option,
        }
        for index, option in enumerate(options)
    ]
    return [{"type": "actions", "elements": elements}]


def options_text_lines(options: list[str]) -> str:
    """The options as bulleted suggestion lines — the text fallback a caller appends
    when the options do not fit native buttons (so they are shown, never dropped)."""
    return "\n".join(f"• {option}" for option in options)


def text_section(text: str) -> dict[str, Any]:
    """A ``section`` block carrying ``text`` as plain_text — the message/question body
    shown above media and option blocks (Slack renders blocks, not the ``text`` field,
    once blocks are present)."""
    return {"type": "section", "text": {"type": "plain_text", "text": text}}
