"""Block Kit builders for the non-form richer sends: display media, a shared
location, and the tappable option vocabulary.

Two option shapes reach this module from two contract seams:

* The ASK path (``deliver``/:class:`~tai42_contract.channels.ChannelDelivery`) still
  carries a plain ``list[str]`` — a select ask's answer set or a text ask's suggested
  replies. Those render through :func:`build_option_blocks` as an ``actions`` block of
  buttons whose ``value`` IS the option text verbatim (mapped straight back on a tap)
  and whose ``action_id`` is ``tai42_select:<index>``.
* The NOTIFY / conversation-answer path (:class:`~tai42_contract.channels.ChannelNotification`)
  carries the TYPED vocabulary — :class:`~tai42_contract.channels.ReplyOption` (tap
  SUBMITS its text), :class:`~tai42_contract.channels.LinkOption` (tap OPENS a url), and
  titled :class:`~tai42_contract.channels.OptionSection` groups. Those render through
  :func:`build_flat_option_blocks` / :func:`build_section_blocks`. A reply button's
  ``action_id`` is ``tai42_reply:<index>`` and its ``value`` is a small JSON envelope
  carrying BOTH the text to submit and the author-set ``id`` (when present) so a tap
  echoes the id back as ``params.reply_id``; a link button's ``action_id`` is
  ``tai42_link:<index>`` and it carries the ``url`` (its tap opens the url and submits
  nothing, so the interactivity door acks it and ignores it).

``media`` (:class:`MediaItem`) renders as native Block Kit: an ``image`` item as an
``image`` block (a public ``https`` url — a ``data:`` image is unrenderable BY NATURE and
raises :class:`~tai42_contract.channels.ChannelInputError`); a ``link`` item as a
``section`` with an mrkdwn link; and a ``document``/``video``/``audio`` item — which Slack
``chat.postMessage`` cannot inline without a file-upload seam this channel does not have —
DEGRADES to a labelled mrkdwn link line carrying the caption/filename, so the file is
reachable and never silently dropped.

Slack buttons carry no per-button description, so a :class:`ReplyOption`'s optional
``description`` is FOLDED into a ``context`` block (the muted small-text line Slack renders
above the buttons) rather than dropped. A ``header`` MediaItem renders through the same
media builder (image block, or a labelled link line for a non-image kind); a ``footer``
renders as a muted ``context`` block; a shared ``location`` renders as a ``section`` naming
the place with an OpenStreetMap link.
"""

from __future__ import annotations

import json
from typing import Any

from tai42_contract.channels import (
    OPTION_ID_MAX_CHARS,
    ChannelInputError,
    LinkOption,
    Option,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind

# The ask-path select/suggested-reply tap: ``value`` is the option text verbatim, the
# index binds it to the option in send order.
SELECT_ACTION_PREFIX = "tai42_select:"
# The notify/answer-path reply-option tap: ``value`` is a JSON envelope (see
# :func:`encode_reply_value`) carrying the submit text and the author-set id.
REPLY_ACTION_PREFIX = "tai42_reply:"
# The notify/answer-path link-option tap: a url button. Its tap opens the url and submits
# nothing, so the interactivity door acks it and ignores it (it is not an option answer).
LINK_ACTION_PREFIX = "tai42_link:"

# Slack Block Kit caps for an actions block of buttons.
_MAX_OPTION_BUTTONS = 25  # elements per actions block
_MAX_BUTTON_TEXT_LEN = 75  # a button's plain_text label
# A button's ``value`` cap (Slack allows 2000). A typical reply envelope (option text plus
# an optional author id) fits comfortably, but a long option text or id can exceed it — so
# the guard drops such an option to the text fallback rather than minting a value Slack
# rejects at send.
_MAX_BUTTON_VALUE_LEN = 2000
# ``alt_text`` is accessibility text (not the media content), so it is bounded, never
# a content field — a caption longer than this is clipped for the alt attribute only.
_MAX_ALT_TEXT_LEN = 2000


def is_select_action(action_id: str) -> bool:
    """Whether ``action_id`` is an ASK-path select/suggested-reply tap (plain-text value)."""
    return action_id.startswith(SELECT_ACTION_PREFIX)


def is_reply_action(action_id: str) -> bool:
    """Whether ``action_id`` is a NOTIFY/answer-path reply-option tap (JSON-envelope value)."""
    return action_id.startswith(REPLY_ACTION_PREFIX)


def is_option_tap(action_id: str) -> bool:
    """Whether ``action_id`` is any option-button tap that SUBMITS a reply — an ask-path
    select tap or a notify reply tap. A link button (``tai42_link:``) opens a url and
    submits nothing, so it is NOT an option tap (the door acks and ignores it)."""
    return is_select_action(action_id) or is_reply_action(action_id)


def encode_reply_value(text: str, option_id: str | None) -> str:
    """The reply button ``value``: a compact JSON envelope of the submit ``text`` and the
    author-set ``option_id`` (omitted when ``None``). Slack echoes ``value`` verbatim on a
    tap, so this is how a stable reply id survives the round trip and returns as
    ``params.reply_id`` (see :func:`decode_reply_value`)."""
    envelope: dict[str, str] = {"text": text}
    if option_id is not None:
        envelope["id"] = option_id
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def decode_reply_value(value: str) -> tuple[str, str | None]:
    """The ``(text, option_id)`` a reply button tap carried, from its JSON-envelope
    ``value``. Defensive: a value that is not our envelope (no JSON object with a string
    ``text``) is treated as plain submit text with no id, so a malformed tap still bridges
    the visible string rather than failing.

    The id is clamped to :data:`~tai42_contract.channels.OPTION_ID_MAX_CHARS` — the same
    bound the contract enforces when an id is minted — so a forged-signed oversized id is
    dropped here (the reply text still bridges) and can never reach ``validate_entry_params``
    to raise and 5xx-loop the webhook."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value, None
    if not isinstance(parsed, dict):
        return value, None
    text = parsed.get("text")
    if not isinstance(text, str) or not text:
        return value, None
    option_id = parsed.get("id")
    if not isinstance(option_id, str) or not option_id or len(option_id) > OPTION_ID_MAX_CHARS:
        return text, None
    return text, option_id


def _plain(text: str) -> dict[str, str]:
    return {"type": "plain_text", "text": text}


def _mrkdwn_section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _escape_mrkdwn(text: str) -> str:
    """Escape the three mrkdwn control characters in author/user text placed in an mrkdwn
    context, so a literal ``&``/``<``/``>`` renders as itself instead of being parsed as
    markup (or opening a stray ``<…>`` link). The ``&`` pass MUST come first — otherwise the
    ``&amp;`` the ``<``/``>`` passes introduce would be double-escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_mrkdwn_url(url: str) -> str:
    """A url placed inside an mrkdwn ``<url|…>`` link needs only its ``&`` escaped (an
    unescaped ``&`` — e.g. the OpenStreetMap ``&mlon`` — is read as an HTML entity); the
    ``<``/``>`` delimiters are ours and the url must keep its own path/query characters."""
    return url.replace("&", "&amp;")


def _link_mrkdwn(item: MediaItem) -> str:
    """A ``link`` media item as an mrkdwn hyperlink (``<url|caption>``)."""
    url = _escape_mrkdwn_url(item.url)
    return f"<{url}|{_escape_mrkdwn(item.caption)}>" if item.caption else f"<{url}>"


def _file_media_mrkdwn(item: MediaItem) -> str:
    """A ``document``/``video``/``audio`` item as a labelled mrkdwn link line.

    Slack ``chat.postMessage`` cannot inline a file bubble without a files.upload seam this
    channel does not have, so a file item degrades to a clickable link labelled by its
    caption (else its filename, else the kind), and a document additionally names its
    filename so the download is identifiable — the file is reachable, never dropped.
    """
    label = item.caption or item.filename or item.kind.value
    line = f"<{_escape_mrkdwn_url(item.url)}|{_escape_mrkdwn(label)}>"
    if item.kind is MediaKind.DOCUMENT and item.filename and item.filename != label:
        line = f"{line} ({_escape_mrkdwn(item.filename)})"
    return line


def build_media_blocks(media: list[MediaItem] | None) -> list[dict[str, Any]]:
    """The display-media blocks for a message: each ``image`` item an image block, each
    ``link`` item an mrkdwn section, each ``document``/``video``/``audio`` item a labelled
    mrkdwn link line (Slack cannot inline those without an upload seam).

    A ``data:`` image has no public url for Slack to fetch, so it is a permanent
    :class:`ChannelInputError` (never a retryable delivery failure) — refused here, before
    any send, so a richer message never posts its text and then fails on an unrenderable
    image.
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
        elif item.kind is MediaKind.LINK:
            blocks.append(_mrkdwn_section(_link_mrkdwn(item)))
        else:  # DOCUMENT / VIDEO / AUDIO
            blocks.append(_mrkdwn_section(_file_media_mrkdwn(item)))
    return blocks


# -- ask-path options (plain list[str]) --------------------------------------


def options_fit_buttons(options: list[str]) -> bool:
    """Whether ``options`` render as a native actions block within Slack's caps
    (button count and per-label length). Past them the caller keeps them as text —
    never a truncated label that would submit a value different from what is shown.
    """
    return len(options) <= _MAX_OPTION_BUTTONS and all(
        len(option) <= _MAX_BUTTON_TEXT_LEN and len(option) <= _MAX_BUTTON_VALUE_LEN for option in options
    )


def build_option_blocks(options: list[str] | None) -> list[dict[str, Any]]:
    """An ``actions`` block of ask-path option buttons when the options fit Slack's caps,
    else an empty list (no options, or past the caps — the caller renders them as text).

    Each button's ``value`` is the option text verbatim (mapped straight back on a tap) and
    its ``action_id`` is ``tai42_select:<index>`` (the interactivity door reads the prefix
    to route the tap; the value carries the answer).
    """
    if not options or not options_fit_buttons(options):
        return []
    elements = [
        {
            "type": "button",
            "action_id": f"{SELECT_ACTION_PREFIX}{index}",
            "text": _plain(option),
            "value": option,
        }
        for index, option in enumerate(options)
    ]
    return [{"type": "actions", "elements": elements}]


def options_text_lines(options: list[str]) -> str:
    """The ask-path options as bulleted suggestion lines — the text fallback a caller
    appends when the options do not fit native buttons (so they are shown, never dropped)."""
    return "\n".join(f"• {option}" for option in options)


# -- notify/answer-path typed options ----------------------------------------


def _option_label(option: Option) -> str:
    return option.label if isinstance(option, LinkOption) else option.text


def _option_fits(option: Option) -> bool:
    # A button label must fit Slack's cap; a reply option additionally mints a JSON value
    # envelope that must fit the value cap. A long option text or author id can push the
    # envelope past the cap, so this guards its length rather than assuming it fits.
    if len(_option_label(option)) > _MAX_BUTTON_TEXT_LEN:
        return False
    if isinstance(option, ReplyOption):
        return len(encode_reply_value(option.text, option.id)) <= _MAX_BUTTON_VALUE_LEN
    return True


def _typed_options_fit(options: list[Option] | list[ReplyOption]) -> bool:
    return len(options) <= _MAX_OPTION_BUTTONS and all(_option_fits(o) for o in options)


def _button_for_option(index: int, option: Option) -> dict[str, Any]:
    if isinstance(option, LinkOption):
        return {
            "type": "button",
            "action_id": f"{LINK_ACTION_PREFIX}{index}",
            "text": _plain(option.label),
            "url": option.url,
        }
    return {
        "type": "button",
        "action_id": f"{REPLY_ACTION_PREFIX}{index}",
        "text": _plain(option.text),
        "value": encode_reply_value(option.text, option.id),
    }


def _descriptions_context(options: list[Option] | list[ReplyOption]) -> list[dict[str, Any]]:
    # Slack buttons carry no description, so a reply option's description is folded into a
    # muted context block above the buttons (never dropped). Only reply options carry one.
    elements = [
        {"type": "mrkdwn", "text": f"*{_escape_mrkdwn(o.text)}* — {_escape_mrkdwn(o.description)}"}
        for o in options
        if isinstance(o, ReplyOption) and o.description
    ]
    return [{"type": "context", "elements": elements}] if elements else []


def _typed_option_lines(options: list[Option] | list[ReplyOption]) -> str:
    lines: list[str] = []
    for option in options:
        if isinstance(option, LinkOption):
            lines.append(f"• <{_escape_mrkdwn_url(option.url)}|{_escape_mrkdwn(option.label)}>")
        else:
            line = f"• {_escape_mrkdwn(option.text)}"
            if option.description:
                line = f"{line} — {_escape_mrkdwn(option.description)}"
            lines.append(line)
    return "\n".join(lines)


def _option_group_blocks(options: list[Option] | list[ReplyOption], start_index: int) -> list[dict[str, Any]]:
    # A group of options as [optional descriptions context] + [actions block of buttons],
    # or a single text-fallback section when a label/value does not fit a button (so the
    # options stay visible, never truncated to submit a different string).
    if not options:
        return []
    if not _typed_options_fit(options):
        # The fallback lines are mrkdwn (a link option renders as an ``<url|label>`` link),
        # so they ride an mrkdwn section — a plain_text section would show the markup
        # literally.
        return [_mrkdwn_section(_typed_option_lines(options))]
    buttons = [_button_for_option(start_index + offset, option) for offset, option in enumerate(options)]
    return [*_descriptions_context(options), {"type": "actions", "elements": buttons}]


def build_flat_option_blocks(options: list[Option] | None) -> list[dict[str, Any]]:
    """The Block Kit blocks for a flat typed option list (reply and/or link buttons),
    with any reply descriptions folded into a preceding context block."""
    if not options:
        return []
    return _option_group_blocks(options, 0)


def build_section_blocks(sections: list[OptionSection] | None) -> list[dict[str, Any]]:
    """The Block Kit blocks for a sectioned option list: each section a titled mrkdwn
    ``section`` header followed by its reply rows as buttons (descriptions folded into a
    context block). A running index keeps every button's ``action_id`` unique across
    sections."""
    if not sections:
        return []
    blocks: list[dict[str, Any]] = []
    index = 0
    for section in sections:
        blocks.append(_mrkdwn_section(f"*{_escape_mrkdwn(section.title)}*"))
        blocks.extend(_option_group_blocks(section.rows, index))
        index += len(section.rows)
    return blocks


def flat_options_text_lines(options: list[Option]) -> str:
    """The flat typed options as bulleted lines for the message text fallback."""
    return _typed_option_lines(options)


def sections_text_lines(sections: list[OptionSection]) -> str:
    """The sectioned options as titled bulleted lines for the message text fallback."""
    blocks: list[str] = []
    for section in sections:
        blocks.append(f"{_escape_mrkdwn(section.title)}:\n{_typed_option_lines(section.rows)}")
    return "\n".join(blocks)


# -- header / footer / location ----------------------------------------------


def build_header_blocks(header: MediaItem | None) -> list[dict[str, Any]]:
    """A header display item shown ABOVE an interactive message: an image block, or a
    labelled link line for a non-image kind (the header is never a ``link``, per the
    contract). Reuses the media builder — a ``data:`` header image is refused here too."""
    if header is None:
        return []
    return build_media_blocks([header])


def build_footer_block(footer: str) -> dict[str, Any]:
    """A footer as a muted ``context`` block — the short trailing line under an interactive
    message."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _escape_mrkdwn(footer)}]}


def _fmt_coord(value: float) -> str:
    # Fixed-precision so a near-zero coordinate renders as a plain decimal (e.g. ``0.0000001``)
    # rather than scientific notation (``1e-07``), which OpenStreetMap would not parse;
    # trailing zeros (and a bare trailing dot) are trimmed so a short coordinate stays clean.
    return f"{value:.7f}".rstrip("0").rstrip(".")


def _osm_url(latitude: float, longitude: float) -> str:
    # An OpenStreetMap pin+map link at the shared coordinates (Slack cannot render a native
    # map, so the coordinates degrade to a clickable map link the human opens).
    lat, lon = _fmt_coord(latitude), _fmt_coord(longitude)
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"


def build_location_block(location: LocationElement) -> dict[str, Any]:
    """A shared geographic point as a ``section``: the optional place name (bold) and
    address, then an OpenStreetMap link to the coordinates."""
    lines: list[str] = []
    if location.name:
        lines.append(f"*{_escape_mrkdwn(location.name)}*")
    if location.address:
        lines.append(_escape_mrkdwn(location.address))
    osm = _escape_mrkdwn_url(_osm_url(location.latitude, location.longitude))
    lines.append(f"<{osm}|View on OpenStreetMap>")
    return _mrkdwn_section("\n".join(lines))


def location_text_line(location: LocationElement) -> str:
    """A one-line location summary for the message text fallback."""
    parts = [_escape_mrkdwn(part) for part in (location.name, location.address) if part]
    # The OSM url is a bare (auto-linked) url here, not inside an ``<url|…>`` link, so it
    # keeps its literal ``&`` — escaping it would break the query Slack hands the browser.
    parts.append(_osm_url(location.latitude, location.longitude))
    return " — ".join(parts)


def text_section(text: str) -> dict[str, Any]:
    """A ``section`` block carrying ``text`` as plain_text — the message/question body
    shown above media and option blocks (Slack renders blocks, not the ``text`` field,
    once blocks are present)."""
    return {"type": "section", "text": _plain(text)}
