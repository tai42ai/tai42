"""Request/response wire shapes for the web-chat doors, and the pure validators that
bound a visitor-sent value before any door forwards it."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from tai42_contract.channels import OPTION_ID_MAX_CHARS

# A web route identity is a URL segment naming a configured route; longer than this
# is not one.
_MAX_IDENTITY_CHARS = 256
# The longest single visitor message the door accepts. Well under the body cap, so
# an over-long text is a precise 422 rather than an opaque 413.
_MAX_TEXT_CHARS = 8000
# The longest string answer the door forwards — it is persisted verbatim into the
# transcript frame the page replays.
_MAX_ANSWER_CHARS = 8000
# A scalar answer (text / confirm / select) is one of these. ``bool`` is listed for
# the reader; it is already an ``int`` subclass.
_ANSWER_TYPES = (str, bool, int, float)
# The largest form answer the door forwards, measured as its serialized UTF-8 bytes.
# A form answer is a structured object rather than one scalar, so it is bounded by
# total size instead of the scalar cap's character count. Kept well under the default
# request body cap (``max_body_bytes``), exactly as the scalar cap is: an over-large
# object is then a precise 422 rather than an opaque 413. This bounds only what is
# forwarded — the callback door validates the object against the question's schema.
_MAX_ANSWER_OBJECT_BYTES = 32 * 1024

# The retry key a page may put on a message POST: opaque to the server, and the only
# thing that makes a re-POST resolve to the turn the lost first attempt started.
_CLIENT_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


_IDENTITY_REQUIREMENT = (
    f"identity must be a non-blank, ':'-free web route identity of at most {_MAX_IDENTITY_CHARS} characters"
)

# The longest operator label a minted code may carry — bounded so an authed door
# cannot store an unbounded string.
_MAX_LABEL_CHARS = 256


def _clean_identity(value: str) -> str | None:
    """The bridge's canonical identity — trimmed, because the bridge trims and an
    untrimmed one here would key a transcript nothing ever writes to. ``None`` when
    it is blank, over-long, or carries the ``:`` that separates a composite recipient
    and qualifies the transcript key — an absent one is the caller's to tell apart."""
    identity = value.strip()
    if not identity or len(identity) > _MAX_IDENTITY_CHARS or ":" in identity:
        return None
    return identity


class IdentityBody(BaseModel):
    """The web route a request names. Every door that takes one canonicalises it the
    same way, so the value compared against the caller's session registration is the
    bridge's own form and not the caller's spacing."""

    identity: str

    @field_validator("identity")
    @classmethod
    def _canonical_identity(cls, value: str) -> str:
        identity = _clean_identity(value)
        if identity is None:
            raise ValueError(_IDENTITY_REQUIREMENT)
        return identity


class RotateBody(IdentityBody):
    """The web route the fresh session is minted for — a session is bound to one.

    ``entry_code`` carries the URL's ``tai_entry`` value: a gated identity refuses a
    rotation with a missing/dead code exactly as the page door refuses an entry; an
    ungated identity ignores the field."""

    entry_code: str | None = None


class GateToggleBody(BaseModel):
    """The management PUT body: whether the route is gated."""

    enabled: bool


class MintCodeBody(BaseModel):
    """The management mint body: an optional operator label and an optional expiry.
    ``expires_at`` must be timezone-aware and in the future (the code's Redis TTL is
    derived from it)."""

    label: str | None = Field(default=None, max_length=_MAX_LABEL_CHARS)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _future_and_tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value


def _validated_retry_key(value: str | None) -> str | None:
    """The one shape rule for a retry key, shared by every body that carries one."""
    if value is not None and _CLIENT_MESSAGE_ID.match(value) is None:
        raise ValueError("client_message_id must be 8 to 64 characters of [A-Za-z0-9_-]")
    return value


class MessageBody(IdentityBody):
    text: str = Field(max_length=_MAX_TEXT_CHARS)
    # Optional retry key. A POST whose reply never reached the browser is re-sent
    # with the SAME key, and the door then derives the bridge's dedup id from it, so
    # the retry resolves to the first attempt's turn instead of delivering twice.
    client_message_id: str | None = None
    # The author-set id of the reply OPTION the visitor tapped, when this message is a
    # chip tap (a media card's reply option carrying an ``id``). It rides the turn as
    # opaque enrichment under ``params.reply_id`` — the SAME convention every channel
    # keeps for a tapped reply id — so the flow reads which option was chosen, not only
    # its echoed text. Absent on a typed message. Bounded by the contract's option-id
    # cap and shaped as a single-line token (it becomes an entry-param value).
    reply_id: str | None = None

    @field_validator("client_message_id")
    @classmethod
    def _opaque_retry_key(cls, value: str | None) -> str | None:
        return _validated_retry_key(value)

    @field_validator("reply_id")
    @classmethod
    def _reply_id_valid(cls, value: str | None) -> str | None:
        # The same shape the contract's ReplyOption.id enforces on the authoring side:
        # a non-blank single-line token within the option-id cap. It rides the wire and
        # becomes an entry-param value, so a newline or control character is refused.
        if value is None:
            return None
        if not value.strip():
            raise ValueError("reply_id must be non-blank when present")
        if len(value) > OPTION_ID_MAX_CHARS:
            raise ValueError(f"reply_id must be at most {OPTION_ID_MAX_CHARS} characters")
        if any(ch.isspace() or not ch.isprintable() for ch in value):
            raise ValueError("reply_id must be a single-line token with no whitespace or control characters")
        return value


class FormSubmissionBody(BaseModel):
    """One ask-less form submission: the values object and an optional retry key.

    ``values`` must be a NON-EMPTY JSON object — a transport-shape rule ({} carries
    nothing to bridge and would render a blank message), never schema validation:
    nothing in this door checks the values against the form's stored schema."""

    values: dict[str, Any]
    client_message_id: str | None = None

    @field_validator("values")
    @classmethod
    def _non_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("values must carry at least one field")
        return value

    @field_validator("client_message_id")
    @classmethod
    def _opaque_retry_key(cls, value: str | None) -> str | None:
        return _validated_retry_key(value)


# -- response bodies (the inner payload each door wraps in ``{"data": ...}``) ----


class MessageAcceptedResponse(BaseModel):
    """Ack of a bridged inbound turn — the id the transcript keys the message on.
    Shared by the messages door and the form-submission door (same wire shape)."""

    message_id: str


class AnswerResultResponse(BaseModel):
    """Ack of a forwarded answer that the callback door accepted and recorded."""

    status: Literal["answered"]


class SessionRotatedResponse(BaseModel):
    """Ack of a session rotation. The fresh session token rides a Set-Cookie header,
    not this body."""

    status: Literal["rotated"]


class CodeView(BaseModel):
    """One minted entry code as the management door lists it — its id and metadata,
    never the raw code. ``created_at``/``expires_at`` are ISO-8601 strings as stored
    (mirrors the ``EntryCode`` record)."""

    code_id: str
    label: str | None
    created_at: str
    expires_at: str | None


class GateStateResponse(BaseModel):
    """A web route's entry-gate state: whether it is gated and its live codes."""

    enabled: bool
    codes: list[CodeView]


class GateToggledResponse(BaseModel):
    """Ack of a gate toggle — echoes the flag now in force."""

    enabled: bool


class MintedCodeResponse(BaseModel):
    """A freshly minted entry code. ``code`` is the raw code, returned this once only
    (a one-time secret — only its hash is stored). ``expires_at`` is an ISO-8601
    string, or ``None`` for a code with no expiry."""

    code: str
    code_id: str
    expires_at: str | None


class CodeRevokedResponse(BaseModel):
    """Ack of an entry code revocation."""

    status: Literal["revoked"]


def _object_refusal(value: dict[str, Any], what: str) -> str | None:
    """The ONE transport bound on a visitor-sent JSON object — the answer door's form
    answer and the form door's values ride it alike; ``what`` names the field in the
    refusal. ``None`` when the object is forwardable.

    ``allow_nan=False`` rejects a non-finite float nested in the object:
    ``json.loads`` accepts ``Infinity``/``NaN``, and forwarding one emits invalid
    JSON downstream AND persists a bad token into the transcript, which then fails
    the page's own JSON parse on every reconnect for the whole transcript TTL. The
    dump also measures the exact forwarded bytes. ``RecursionError`` is caught with
    the same refusal: the encoder's depth limit is its own, so an object the parse
    admitted can still overrun it here."""
    try:
        serialized = json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError):
        return f"{what} must contain only finite numbers"
    if len(serialized.encode("utf-8")) > _MAX_ANSWER_OBJECT_BYTES:
        return f"{what} must serialize to at most {_MAX_ANSWER_OBJECT_BYTES} bytes"
    return None


def _answer_refusal(answer: Any) -> str | None:
    """The refusal for an answer value this door will not forward, or ``None``.

    A scalar answer (text/confirm/select) is one string, number, or boolean; a form
    answer is a JSON object bounded by its serialized size. The callback door stays
    authoritative on format and schema match — this only bounds what is forwarded."""
    if isinstance(answer, dict):
        return _object_refusal(answer, "answer object")
    if not isinstance(answer, _ANSWER_TYPES):
        return "answer must be a string, number, boolean, or object"
    if isinstance(answer, float) and not math.isfinite(answer):
        # ``json.loads`` accepts ``Infinity``/``NaN`` and overflows ``1e999`` to inf.
        # Forwarding one emits invalid JSON to the callback door AND persists a
        # ``Infinity`` token into the transcript, which then fails the page's own
        # JSON parse on every reconnect for the whole transcript TTL.
        return "answer must be a finite number"
    if isinstance(answer, str) and len(answer) > _MAX_ANSWER_CHARS:
        return f"answer must be at most {_MAX_ANSWER_CHARS} characters"
    return None
