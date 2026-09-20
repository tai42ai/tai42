"""``ChannelNotification`` — one fire-and-forget message handed to a channel + its caps."""

from __future__ import annotations

import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tai42_contract.channels.composition import check_interactive_composition
from tai42_contract.channels.options import (
    Option,
    OptionSection,
    check_footer,
    check_header,
    check_options,
    check_sections,
)
from tai42_contract.channels.templates import ChannelTemplate
from tai42_contract.interactions.models import FormData, FormPage, LocationElement, MediaItem, check_media_list

# Generous abuse bound on a notification's message text — not a UX limit; channels may
# impose tighter limits. Caps what persists into replayed transcript streams.
NOTIFICATION_MESSAGE_MAX_CHARS = 65536

# Abuse bound on a notification address (``recipient``, ``sender_identity``) and the sink's
# ``audience`` identity — short routing values, not message bodies. Each persists into the
# replayed feed record and ``audience`` also becomes a per-identity Redis key, so an
# unbounded value would bloat a stored frame or mint an oversized key.
NOTIFICATION_ADDRESS_MAX_CHARS = 512


with warnings.catch_warnings():
    # The ``schema`` field intentionally shadows pydantic's deprecated
    # ``BaseModel.schema()`` alias (the current API is ``model_json_schema()``);
    # the field name matches the JSON-schema payload it carries. Suppress the
    # shadow warning at the definition site so every importer is safe regardless
    # of its own warnings config — narrowly matched, never a blanket ignore.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class ChannelNotification(BaseModel):
        """One fire-and-forget message handed to a channel.

        A notification carries no interaction, no ticket, no ``callback_url`` and
        no deadline: the channel sends the message and nothing travels back.
        ``recipient`` is the OPTIONAL caller-requested address (chat id, phone
        number, ...): the channel plugin validates it against its operator-set
        allowlist and refuses to send to an unlisted address; when omitted the
        plugin sends to its operator-configured default recipient. It is an
        address only, never a secret or credential.

        ``sender_identity`` is the OPTIONAL address to send FROM when the channel fronts
        several operator identities: an internal routing control set by the sending side,
        never caller-supplied, and an address only — never a secret.

        ``message`` is the human-readable text, non-blank BY DEFAULT — EXCEPT it may be the empty
        string ``""`` for a CONTENT-ONLY send: a caption-less bubble that is just ``media`` or a
        ``location``, with no text carrier. The admissible states are "``message`` non-blank" OR
        "blank ``message`` WITH non-empty ``media`` OR a ``location``"; a blank ``message`` with no
        such content has nothing to deliver and is refused. (``message`` stays REQUIRED — a
        content-only sender passes ``""`` explicitly — because every caller constructs it in code
        with the text in hand.) An interactive surface (``options``, ``sections`` or ``schema``)
        REQUIRES a non-blank ``message`` — a choice or a form needs a prompt — so a content-only
        send carries none; a ``template`` likewise rides a non-blank ``message``.

        The OPTIONAL richer-send forms reuse the same ``message`` as the human-readable
        equivalent. ``media`` is display media the channel sends alongside the message (reusing
        :class:`MediaItem`, image/document/video/audio/link); a present list is non-empty.
        ``location`` is a shared geographic point (:class:`LocationElement`). ``template`` sends a
        pre-approved :class:`ChannelTemplate` for out-of-window delivery. ``options`` is a flat
        list of tappable :data:`Option` entries — a :class:`ReplyOption` (a tap submits its text
        as a visitor message) or a :class:`LinkOption` (a tap opens its url) — at most
        ``NOTIFICATION_OPTIONS_MAX``. ``sections`` is the sectioned alternative: titled
        :class:`OptionSection` groups of reply rows (rows summed across sections stay within
        ``NOTIFICATION_OPTIONS_MAX``). ``header`` is a single display-media header and ``footer`` a
        short trailing line, each composing an interactive message (they REQUIRE ``options`` or
        ``sections``).

        The composition rules (:func:`check_interactive_composition`): ``options`` XOR
        ``sections`` (one choice surface); ``schema`` excludes both (one interactive surface); a
        ``template`` is the standalone out-of-window send, MUTUALLY EXCLUSIVE with every other
        content and interactive field; ``options``/``sections`` and ``schema`` MAY each combine
        with ``media`` and ``location``. A channel that does not advertise the matching capability
        flag (``supports_media_notifications`` / ``supports_location_notifications`` /
        ``supports_template_notifications`` / ``supports_interactive_notifications`` /
        ``supports_form_notifications``, the OPTIONAL class-attribute convention documented on
        :class:`Channel`) never receives the matching field.

        ``schema`` is the form answer schema for an ASK-LESS FORM: the channel renders
        ``message`` as the form's prompt and ``schema`` as the fillable form, and the
        participant's submission enters the conversation as a participant message — no interaction,
        no ticket, no callback, the same inbound path a tapped option takes. A present
        ``schema`` is a non-empty dict; its deep shape is the sender's shared
        channel-deliverable subset walk (the same split :class:`ChannelDelivery` keeps),
        never re-checked here. ``schema`` REQUIRES a non-blank ``message`` — a form needs
        a prompt — and is MUTUALLY EXCLUSIVE with ``template`` and with ``options`` (one
        message carries ONE interactive surface); it MAY combine with ``media``. It rides
        the ``supports_form_notifications`` capability flag, and a form channel's OPTIONAL
        ``validate_form_schema(schema, question)`` hook (see :class:`Channel`) is reused
        at notify time with this ``message`` as the ``question`` argument, so the
        channel's own form limits refuse an unrenderable form before the send. Some channels
        also constrain WHEN a form may be sent: WhatsApp delivers a notify-form only inside
        the provider's customer-service window, and an out-of-window send fails loudly at the
        channel — never silently downgraded.
        """

        model_config = ConfigDict(frozen=True)

        message: str  # human-readable text; blank ("") ONLY for a content-only send (media/location carries it)
        recipient: str | None = None  # caller-requested address; None -> plugin default
        sender_identity: str | None = None  # internal sending identity; None -> plugin default
        media: list[MediaItem] | None = None  # display media sent WITH the message; None -> none
        location: LocationElement | None = None  # a shared geographic point; None -> none
        template: ChannelTemplate | None = None  # out-of-window template send; None -> freeform
        options: list[Option] | None = None  # flat tappable options (reply/link); None -> none
        sections: list[OptionSection] | None = None  # a sectioned option list; None -> none
        header: MediaItem | None = None  # single media header above an interactive message; None -> none
        footer: str | None = None  # short trailing line under an interactive message; None -> none
        # The form answer schema for an ask-less form; the submission enters the conversation as
        # a participant message. Intentionally named ``schema`` (matches the payload it carries);
        # shadows the deprecated ``BaseModel.schema()`` alias, which this model never uses.
        schema: dict[str, Any] | None = None  # pyright: ignore[reportIncompatibleMethodOverride]
        # Per-send prefill/options over ``schema`` (the same :class:`ChannelDelivery` keeps),
        # so an ask-less form can open already filled in; ride ONLY a form send (``schema``).
        data: FormData | None = None
        # The form's step layout over ``schema``, present only on a form send: each
        # :class:`FormPage` names the top-level properties shown on one step.
        pages: list[FormPage] | None = None

        @field_validator("message")
        @classmethod
        def _message_valid(cls, value: str) -> str:
            # Length cap only; whether a BLANK message is admissible depends on ``media`` (a
            # media-only send carries no text) and is decided in :meth:`_message_or_media` once
            # every field is bound.
            if len(value) > NOTIFICATION_MESSAGE_MAX_CHARS:
                raise ValueError(
                    f"message must be at most {NOTIFICATION_MESSAGE_MAX_CHARS} characters, got {len(value)}"
                )
            return value

        @field_validator("recipient", "sender_identity")
        @classmethod
        def _address_non_empty(cls, value: str | None) -> str | None:
            if value is not None and not value.strip():
                raise ValueError("must be a non-empty address when present")
            if value is not None and len(value) > NOTIFICATION_ADDRESS_MAX_CHARS:
                raise ValueError(
                    f"address must be at most {NOTIFICATION_ADDRESS_MAX_CHARS} characters, got {len(value)}"
                )
            return value

        @field_validator("media")
        @classmethod
        def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
            # None means no media; the shared list-level caps (non-empty, item count, summed
            # URI) are the same wire-contract bound on the notify REQUEST as on the ask
            # REQUEST — the channel refuses anything beyond its own native envelope.
            if value is not None:
                check_media_list(value)
            return value

        @field_validator("options")
        @classmethod
        def _options_valid(cls, value: list[Option] | None) -> list[Option] | None:
            # None means no options; a present flat list is non-empty and capped. Each option's own
            # shape (reply text / link label+url) is the ReplyOption/LinkOption concern.
            return check_options(value)

        @field_validator("sections")
        @classmethod
        def _sections_valid(cls, value: list[OptionSection] | None) -> list[OptionSection] | None:
            return check_sections(value)

        @field_validator("footer")
        @classmethod
        def _footer_valid(cls, value: str | None) -> str | None:
            return check_footer(value)

        @field_validator("header")
        @classmethod
        def _header_valid(cls, value: MediaItem | None) -> MediaItem | None:
            return check_header(value)

        @field_validator("schema")
        @classmethod
        def _schema_non_empty(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
            # None means no form; a present schema is a non-empty dict — the same bound the
            # ask-path ChannelDelivery enforces. The deep shape is the sender's shared
            # channel-deliverable subset walk, never re-implemented here.
            if value is not None and not value:
                raise ValueError("schema must be a non-empty dict when present")
            return value

        @model_validator(mode="after")
        def _check_composition(self) -> ChannelNotification:
            # The shared cross-field interactive-composition rules, so this carrier and AnswerPart
            # can never drift (see :func:`check_interactive_composition`).
            check_interactive_composition(
                message=self.message,
                media=self.media,
                location=self.location,
                template=self.template,
                options=self.options,
                sections=self.sections,
                schema=self.schema,
                header=self.header,
                footer=self.footer,
                noun="notification",
            )
            return self

        @model_validator(mode="after")
        def _check_form_extras(self) -> ChannelNotification:
            # ``data``/``pages`` enrich a form send's ``schema`` and mean nothing without it, so
            # they ride ONLY a notification that carries a ``schema`` — present on any other send
            # is a caller bug refused loudly, mirroring :class:`ChannelDelivery`. The deep prefill
            # cross-check against the schema is the sender's (the :class:`AnswerPart` /
            # ``InteractionRequest`` seam), never re-run here.
            if self.schema is None:
                if self.data is not None:
                    raise ValueError("a notification with no schema carries no form data")
                if self.pages is not None:
                    raise ValueError("a notification with no schema carries no form pages")
            return self
