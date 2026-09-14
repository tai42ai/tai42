"""Interactive tappable-option shapes and their list-level caps.

Reply/link options, sectioned lists, and the ``check_*`` helpers a carrier runs over an
option/section/header/footer surface.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tai42_contract.interactions.models import MEDIA_CAPTION_MAX_CHARS, MediaItem, MediaKind, validate_action_url

# Upper bound on a notification's tappable option list — one notification cannot fan
# out an unbounded set of tappable options; matches the richest interactive-list medium.
NOTIFICATION_OPTIONS_MAX = 10

# Per-option character bound — same cap as the media caption, the other short label that
# rides a replayed frame.
NOTIFICATION_OPTION_MAX_CHARS = MEDIA_CAPTION_MAX_CHARS

# Upper bound on a sectioned option list's section count. A sectioned list groups its rows
# under titled sections; the summed row count across every section still obeys
# ``NOTIFICATION_OPTIONS_MAX`` (one message cannot fan out an unbounded tap set).
NOTIFICATION_SECTIONS_MAX = 10

# Per-footer character bound — the short trailing line under an interactive message; same cap
# as an option label, the other short rider on an interactive frame.
NOTIFICATION_FOOTER_MAX_CHARS = MEDIA_CAPTION_MAX_CHARS

# Bound on an AUTHORED reply-option id — the stable id a sender may set on a quick-reply button
# or a list row so the tap echoes it back verbatim. Set to the STRICTEST carrier's cap (WhatsApp
# limits an interactive button/row id to 256 characters); a channel that mints its own id when
# the author sets none is unaffected.
OPTION_ID_MAX_CHARS = 256


class ReplyOption(BaseModel):
    """A tappable suggested reply. Tapping SUBMITS ``text`` as the participant's next inbound message —
    the quick-reply / list-row case, where the option's own text becomes the turn. ``description``
    is an OPTIONAL secondary line a sectioned-list row renders under its ``text``; a channel that
    renders flat buttons (no descriptions) ignores it.

    ``id`` is an OPTIONAL author-set stable identifier for the button/list row. When set, a channel
    sends it verbatim on the wire and the participant's tap echoes it back (a channel surfaces the echoed
    id to the inbound turn as opaque enrichment — e.g. Slack forwards it as ``params.reply_id``);
    when ``None`` the channel mints its own id as today. Bounded by ``OPTION_ID_MAX_CHARS`` and a
    single-line non-blank label — the strictest carrier's rule. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["reply"] = "reply"
    text: str
    description: str | None = None
    id: str | None = None

    @field_validator("text")
    @classmethod
    def _text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply option text must be non-blank")
        if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
            raise ValueError(
                f"reply option text must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("reply option description must be non-blank when present")
            if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
                raise ValueError(
                    f"reply option description must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, "
                    f"got {len(value)}"
                )
        return value

    @field_validator("id")
    @classmethod
    def _id_valid(cls, value: str | None) -> str | None:
        # An author-set id is a single-line non-blank token within the strictest carrier's cap;
        # raw whitespace and control/format characters are rejected (an id rides the wire and is
        # echoed back, so it must not carry a newline or a bidi spoof).
        if value is not None:
            if not value.strip():
                raise ValueError("reply option id must be non-blank when present")
            if len(value) > OPTION_ID_MAX_CHARS:
                raise ValueError(f"reply option id must be at most {OPTION_ID_MAX_CHARS} characters, got {len(value)}")
            if any(ch.isspace() or not ch.isprintable() for ch in value):
                raise ValueError("reply option id must be a single-line token with no whitespace or control characters")
        return value


class LinkOption(BaseModel):
    """A tappable link action. Tapping OPENS ``url`` (an absolute ``http(s)`` URL) in the human's
    browser — NO message is submitted, distinct from a :class:`ReplyOption`. ``label`` is the
    button text. The URL-button / call-to-action case. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["link"] = "link"
    label: str
    url: str

    @field_validator("label")
    @classmethod
    def _label_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("link option label must be non-blank")
        if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
            raise ValueError(
                f"link option label must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("url")
    @classmethod
    def _url_valid(cls, value: str) -> str:
        return validate_action_url(value)


# One tappable option on an interactive message: EITHER a reply (tap submits its text) or a link
# action (tap opens its url). A discriminated union on ``kind`` — the input carries the tag, so a
# bare string is not an option (an option
# is authored as ``{"kind": "reply", "text": …}`` or ``{"kind": "link", "label": …, "url": …}``).
Option = Annotated[ReplyOption | LinkOption, Field(discriminator="kind")]


class OptionSection(BaseModel):
    """One titled section of a sectioned option list. ``title`` is the section header; ``rows`` are
    its entries — a sectioned list holds :class:`ReplyOption` rows ONLY (a tapped row submits its
    text; a link action is a button, never a list row). A present ``rows`` is non-empty. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    rows: list[ReplyOption]

    @field_validator("title")
    @classmethod
    def _title_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("section title must be non-blank")
        if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
            raise ValueError(
                f"section title must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("rows")
    @classmethod
    def _rows_non_empty(cls, value: list[ReplyOption]) -> list[ReplyOption]:
        if not value:
            raise ValueError("section rows must be a non-empty list")
        return value


def check_options(value: list[Option] | None) -> list[Option] | None:
    """List-level caps on a flat interactive option list: None means none; a present list is
    non-empty and holds at most ``NOTIFICATION_OPTIONS_MAX`` entries. Each option's own shape
    (reply text / link label+url bounds) is the :class:`ReplyOption`/:class:`LinkOption` concern.
    Raises ``ValueError``."""
    if value is None:
        return None
    if not value:
        raise ValueError("options must be a non-empty list when present")
    if len(value) > NOTIFICATION_OPTIONS_MAX:
        raise ValueError(f"options carries at most {NOTIFICATION_OPTIONS_MAX} entries, got {len(value)}")
    return value


def check_sections(value: list[OptionSection] | None) -> list[OptionSection] | None:
    """List-level caps on a sectioned option list: None means none; a present list is non-empty,
    holds at most ``NOTIFICATION_SECTIONS_MAX`` sections, and its rows summed across every section
    stay within ``NOTIFICATION_OPTIONS_MAX`` (one message never fans out an unbounded tap set).
    Raises ``ValueError``."""
    if value is None:
        return None
    if not value:
        raise ValueError("sections must be a non-empty list when present")
    if len(value) > NOTIFICATION_SECTIONS_MAX:
        raise ValueError(f"sections carries at most {NOTIFICATION_SECTIONS_MAX} sections, got {len(value)}")
    total_rows = sum(len(section.rows) for section in value)
    if total_rows > NOTIFICATION_OPTIONS_MAX:
        raise ValueError(
            f"sections carry at most {NOTIFICATION_OPTIONS_MAX} rows in total across all sections, got {total_rows}"
        )
    return value


def check_footer(value: str | None) -> str | None:
    """A footer is the short trailing line under an interactive message: None means none; a
    present value is non-blank and within ``NOTIFICATION_FOOTER_MAX_CHARS``. Raises ``ValueError``."""
    if value is None:
        return None
    if not value.strip():
        raise ValueError("footer must be non-blank when present")
    if len(value) > NOTIFICATION_FOOTER_MAX_CHARS:
        raise ValueError(f"footer must be at most {NOTIFICATION_FOOTER_MAX_CHARS} characters, got {len(value)}")
    return value


def check_header(value: MediaItem | None) -> MediaItem | None:
    """A header is a SINGLE display-media item shown above an interactive message: None means
    none; a present item is display media (image/document/video/audio), never a ``link`` (an
    anchor is content, not a header). The item's own url/kind shape is :class:`MediaItem`'s
    concern. Raises ``ValueError``."""
    if value is not None and value.kind is MediaKind.LINK:
        raise ValueError("header media must be a display item (image/document/video/audio), not a link")
    return value
