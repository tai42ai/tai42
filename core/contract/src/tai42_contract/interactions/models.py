"""Pydantic v2 models for the ``ask_user`` interactions capability.

``InteractionRequest`` is the durable question written to a per-group stream;
``InteractionResponse`` is the validated answer pushed onto the reply channel;
``InteractionState`` is the mutable record the answer endpoint reads to guard
against a duplicate answer and to validate the submitted value.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator, model_validator


class AnswerFormat(StrEnum):
    TEXT = "text"
    CONFIRM = "confirm"
    SELECT = "select"
    FORM = "form"
    EXTERNAL = "external"


class MediaKind(StrEnum):
    IMAGE = "image"
    LINK = "link"


# Caps on the media attached to one question. They bound the durable record and
# the SSE frame it is replayed in — a wire-contract property, not an operator
# preference — so they are constants, never settings. MEDIA_MAX_ITEMS bounds the
# item count; MEDIA_URL_MAX_CHARS is a sane single-URL length; MEDIA_DATA_URI_MAX_CHARS
# bounds an inline data: URI (~512 KiB of text, ~384 KiB decoded); MEDIA_CAPTION_MAX_CHARS
# bounds the alt-text/label; MEDIA_TOTAL_URI_CHARS is the per-question budget for the
# summed URI text across all items — the backlog replays the whole pending index to every
# client on every reconnect, so a per-question ceiling bounds a bytes x pending x clients
# amplification the per-item cap alone does not.
MEDIA_MAX_ITEMS = 8
MEDIA_URL_MAX_CHARS = 8192
MEDIA_DATA_URI_MAX_CHARS = 524_288
MEDIA_CAPTION_MAX_CHARS = 1000
MEDIA_TOTAL_URI_CHARS = 1_048_576

_DATA_IMAGE_PREFIX = "data:image/"

_DNS_LABEL = re.compile(r"[A-Za-z0-9-]{1,63}")


def _label_is_numberish(label: str) -> bool:
    # A label the browser's host parser would read as a trailing number: all ASCII
    # digits (decimal/octal), or a ``0x``/``0X`` hex literal — the bare prefix included,
    # which the parser reads as IPv4 zero. Reaching here means it is not a valid
    # dotted-quad IPv4, so a numberish final label is an IPv4-lookalike
    # (``999.999.999.999``, ``4294967296``, ``0x100000000``, ``0x``) — reject.
    if label.isdigit():
        return True
    low = label.lower()
    return low.startswith("0x") and all(c in "0123456789abcdef" for c in low[2:])


def _is_valid_host(host: str, *, bracketed: bool) -> bool:
    # Host must be ASCII and one of: a bracketed IPv6 literal, a dotted-quad IPv4,
    # or ASCII DNS labels (1-63 of [A-Za-z0-9-], no leading/trailing hyphen,
    # total <=253, one trailing dot allowed, final label not numberish). Any ``%``
    # is rejected. Deliberately stricter than the WHATWG parser so a divergence
    # fails loud at send, never silently at render.
    if "%" in host:
        # Rejected universally, before either literal parse: a bracketed zone-id
        # (``[fe80::1%eth0]``) is forbidden by WHATWG, and ``ipaddress.IPv6Address``
        # would otherwise accept it.
        return False
    if bracketed:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        pass
    name = host[:-1] if host.endswith(".") else host
    if not name or len(name) > 253:
        return False
    labels = name.split(".")
    for label in labels:
        if not _DNS_LABEL.fullmatch(label) or label.startswith("-") or label.endswith("-"):
            return False
    return not _label_is_numberish(labels[-1])


def _is_absolute_web_url(value: str, *, schemes: tuple[str, ...]) -> bool:
    # An absolute URL parses to a scheme in ``schemes``, a valid ASCII host, an
    # in-range port, and NO embedded userinfo. A bare ``"https://"`` (no host), a
    # scheme-only/relative string, a ``"https://user@host"`` credential form (the
    # ``trusted.com@evil.com`` authority-spoofing vector), an out-of-range port
    # (``.port`` raises ValueError past 65535 — a spec-invalid URL the browser's
    # parser would reject on replay), or a malformed authority (an unterminated
    # IPv6 literal makes ``urlsplit`` raise) is all False. Host validity is decided
    # by ``_is_valid_host`` (IDN callers supply punycode; Unicode hosts are False).
    # Parsing (not ``startswith``) is what rejects these.
    try:
        split = urlsplit(value)
        _ = split.port
    except ValueError:
        return False
    if split.scheme not in schemes or "@" in split.netloc:
        return False
    host = split.hostname
    if not host:
        return False
    return _is_valid_host(host, bracketed="[" in split.netloc)


class MediaItem(BaseModel):
    """One media item shown WITH a question — display-only, never part of the answer.

    ``kind`` selects how it renders: an ``image`` inline, a ``link`` as a labelled
    anchor. ``url`` is the source — an ``image`` must be an absolute ``https`` URL or a
    ``data:image/*`` URI (remote images are https-only: the inbox CSP ``img-src`` admits
    ``https:``/``data:`` but not ``http:``, so an ``http:`` image would be an unrenderable
    record), while a ``link`` must be an absolute ``http(s)`` URL (anchors are not governed
    by ``img-src``; the human clicks through). A remote url names a host directly —
    an ASCII DNS name, dotted-quad IPv4, or bracketed IPv6 (IDN callers supply
    punycode); an embedded ``user@`` credential form is rejected as it spoofs the
    authority — and is always a single line — raw whitespace and control/format
    characters are rejected. ``caption`` is the accessibility text — the image's alt text or the
    link's display label.
    """

    kind: MediaKind
    url: str
    caption: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("media url must be non-blank")
        # A URL never carries raw whitespace or control/format/separator characters;
        # rejecting them keeps the validated string identical to the stored one
        # (``urlsplit`` silently strips ``\t\r\n`` and surrounding whitespace) and
        # blocks embedded newlines and bidi/zero-width spoofing in the rendered link.
        if any(ch.isspace() or not ch.isprintable() for ch in value):
            raise ValueError("media url must be a single-line URL with no whitespace or control characters")
        return value

    @field_validator("caption")
    @classmethod
    def _check_caption(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("media caption must be non-blank when present")
            if len(value) > MEDIA_CAPTION_MAX_CHARS:
                raise ValueError(
                    f"media caption must be at most {MEDIA_CAPTION_MAX_CHARS} characters, got {len(value)}"
                )
        return value

    @model_validator(mode="after")
    def _check_url_for_kind(self) -> MediaItem:
        if self.kind is MediaKind.LINK:
            if not _is_absolute_web_url(self.url, schemes=("http", "https")):
                raise ValueError("link media url must be an absolute http(s) URL")
            if len(self.url) > MEDIA_URL_MAX_CHARS:
                raise ValueError(
                    f"link media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(self.url)}"
                )
        elif self.url.startswith(_DATA_IMAGE_PREFIX):
            if len(self.url) > MEDIA_DATA_URI_MAX_CHARS:
                raise ValueError(
                    f"image media data: URI must be at most {MEDIA_DATA_URI_MAX_CHARS} characters, got {len(self.url)}"
                )
        elif _is_absolute_web_url(self.url, schemes=("https",)):
            if len(self.url) > MEDIA_URL_MAX_CHARS:
                raise ValueError(
                    f"image media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(self.url)}"
                )
        else:
            raise ValueError("image media url must be an absolute https URL or a data:image/* URI")
        return self


class InteractionRequest(BaseModel):
    """The durable question. One per stream entry."""

    interaction_id: str
    group_id: str
    question: str
    answer_format: AnswerFormat = AnswerFormat.TEXT
    format_payload: dict[str, Any] | None = None
    reply_to: str
    created_at: datetime
    timeout_at: datetime
    # When set, the answer body is treated as sensitive (credentials, personal
    # data) and is never persisted into the answered state — the blocked caller
    # still receives the full answer through the reply channel, but the durable
    # record keeps only the answered status. Set per question by the tool author.
    sensitive: bool = False
    # Name of the registered channel that delivered this question out-of-band;
    # None means the question surfaced in the inbox only. Set by the asking
    # side when the question is raised with a channel, so consumers can
    # attribute the medium the answer arrived through.
    channel: str | None = None
    # The channel delivery address the asking side passed for this question (a
    # chat id, phone number, ...) — display/binding attribution on the operator
    # feed, a WHERE, never an authorization axis. None when no address was
    # passed; a set value is a non-blank string (the helper rejects blanks).
    recipient: str | None = None
    # The run/thread that raised this question — the background tool-run id
    # stamped when the question is asked inside a tool run, None outside one. It
    # attributes a pending question and lets programmatic answering bind it to
    # the originating run. A set value is a non-blank string.
    origin: str | None = None
    # Identity (a user_id) the interaction is scoped to: a restricted caller sees
    # and answers only questions addressed to its own identity. This is the
    # isolation axis, distinct from ``channel``/``reply_to`` delivery addressing
    # — it is a who, not a where. None means the question is unaddressed
    # (an operator/broadcast question every unrestricted caller may see).
    audience: str | None = None
    # Display-only media rendered WITH the question in the inbox: images and links
    # the human sees when reading the question. It never becomes part of the answer,
    # and it is not forwarded to channel deliveries — the inbox is where it renders.
    # None means no media; a present list is non-empty (a present-but-empty list is
    # a caller bug). Set per question by the tool author.
    media: list[MediaItem] | None = None

    @field_validator("media")
    @classmethod
    def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        if value is not None:
            if not value:
                raise ValueError("media must be a non-empty list when present")
            if len(value) > MEDIA_MAX_ITEMS:
                raise ValueError(f"media carries at most {MEDIA_MAX_ITEMS} items, got {len(value)}")
            total = sum(len(item.url) for item in value)
            if total > MEDIA_TOTAL_URI_CHARS:
                raise ValueError(
                    f"media total url length must be at most {MEDIA_TOTAL_URI_CHARS} characters, got {total}"
                )
        return value

    @field_validator("created_at", "timeout_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # A naive timeout_at compared against an aware ``now()`` raises TypeError
        # at use time; reject it here and normalize to UTC (same strictness as
        # ConnectionRecord).
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_payload(self) -> InteractionRequest:
        if self.answer_format is AnswerFormat.SELECT:
            options = (self.format_payload or {}).get("options")
            if not options:
                raise ValueError("select answer_format requires non-empty options")
        elif self.answer_format is AnswerFormat.FORM:
            if not (self.format_payload or {}).get("schema"):
                raise ValueError("form answer_format requires a schema")
        elif self.answer_format is AnswerFormat.EXTERNAL:
            url = (self.format_payload or {}).get("url")
            # The external surface is reached through this url; a non-str or empty
            # value would leave the caller with no place to send the human.
            if not isinstance(url, str) or not url:
                raise ValueError("external answer_format requires a non-empty string url in format_payload")
        else:
            if self.format_payload is not None:
                raise ValueError(f"{self.answer_format.value} answer_format carries no format_payload")
        return self


class InteractionResponse(BaseModel):
    """The answer. Validated server-side before it wakes the caller."""

    interaction_id: str
    answer: Any
    answered_by: str
    answered_at: datetime


class InteractionState(BaseModel):
    """Mutable companion to the immutable stream entry, keyed by interaction id."""

    status: Literal["pending", "answered"]
    group_id: str
    request: InteractionRequest
    response: InteractionResponse | None = None
