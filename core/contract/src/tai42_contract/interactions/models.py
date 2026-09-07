"""Pydantic v2 models for the ``ask_user`` interactions capability.

``InteractionRequest`` is the durable question written to a per-group stream;
``InteractionResponse`` is the validated answer pushed onto the reply channel;
``InteractionState`` is the mutable record the answer endpoint reads to guard
against a duplicate answer and to validate the submitted value;
``SuspendedInteraction`` is the sentinel an ``async`` ask returns in place of an
answer when it parks the caller.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tai42_contract.entry_params import validate_entry_params
from tai42_contract.states import StateContext


class AnswerFormat(StrEnum):
    TEXT = "text"
    CONFIRM = "confirm"
    SELECT = "select"
    FORM = "form"
    EXTERNAL = "external"


class AnswerMismatchPolicy(StrEnum):
    """What a channel-delivered ask does with a guest reply the answer door REJECTS (a 400 on a
    live ask — the reply did not fit the question's format).

    ``RETRY`` (the default, today's behavior): keep the ask parked and tell the guest what's
    expected so they can answer again in place. ``BRIDGE``: treat an unmatched reply as a
    DIGRESSION — keep the ask parked (no notice), and hand the reply to the conversation as a fresh
    routed turn so the flow handles it; the ask then ends ONLY by a real answer or its timeout,
    never by unmatched input. Set per ask by the tool author; a plain freeform/select ask keeps the
    default.
    """

    RETRY = "retry"
    BRIDGE = "bridge"


class MediaKind(StrEnum):
    IMAGE = "image"
    LINK = "link"
    DOCUMENT = "document"
    VIDEO = "video"
    AUDIO = "audio"


# The media kinds that carry a fetchable file body (as opposed to ``LINK``, a labelled
# anchor the human clicks through). They share one url discipline — an absolute https url, a
# same-origin served-media reference, or an absolute served-media url — and only ``IMAGE``
# additionally admits an inline ``data:image/*`` URI (the inbox CSP ``img-src`` renders it).
FILE_MEDIA_KINDS = frozenset({MediaKind.IMAGE, MediaKind.DOCUMENT, MediaKind.VIDEO, MediaKind.AUDIO})


# Caps on the media attached to one question. They bound the ask/notify REQUEST
# (the input a tool submits), never a replay — a wire-contract property, not an
# operator preference — so they are constants, never settings. MEDIA_MAX_ITEMS is a
# loose platform abuse guard on the item count: each channel refuses anything beyond
# its own native envelope, so this ceiling only stops a pathological ask.
# MEDIA_URL_MAX_CHARS is a sane single-URL length; MEDIA_DATA_URI_MAX_CHARS bounds an
# inline data: URI (~512 KiB of text, ~384 KiB decoded); MEDIA_CAPTION_MAX_CHARS
# bounds the alt-text/label; MEDIA_TOTAL_URI_CHARS is the per-request budget for the
# summed URI text across all items.
MEDIA_MAX_ITEMS = 50
MEDIA_URL_MAX_CHARS = 8192
MEDIA_DATA_URI_MAX_CHARS = 524_288
MEDIA_CAPTION_MAX_CHARS = 1000
MEDIA_TOTAL_URI_CHARS = 1_048_576

# A document media item's suggested display filename (``document.pdf``): a short single-line
# label the medium shows for the download, never a filesystem path. Meaningful only for a
# DOCUMENT item; the model rejects it on any other kind.
MEDIA_FILENAME_MAX_CHARS = 255

# Caps on a shared location element's optional labels — a place name and a street address, each
# a short single-line human label the medium renders beside the pin, not a message body.
LOCATION_NAME_MAX_CHARS = 1000
LOCATION_ADDRESS_MAX_CHARS = 1000

# Cap on a per-ask custom mismatch notice — the guest-facing rejection text a ``retry``-policy ask
# may substitute for the built-in one. A single guest reply, so a small bound (channels impose
# their own tighter message caps); matches the conversation route's ``error_reply_text`` bound.
MISMATCH_NOTICE_MAX_CHARS = 2000

# Cap on the question text stored verbatim into the interaction state hash and the
# per-group stream — a prompt authored by a server tool, so a few KB is generous.
# Bounds the durable record and its replay, a wire-contract property, so it is a
# constant, never a setting; an over-cap question is refused loudly.
QUESTION_MAX_CHARS = 8192

_DATA_IMAGE_PREFIX = "data:image/"

# URL path segment under which the skeleton serves media stored by reference.
# An ``image`` url of the form ``{MEDIA_ROUTE_PREFIX}{id}`` (relative, same origin)
# is a stored-media reference — a valid image url. Shared by the skeleton (the
# serve route) and the channels (absolute-url minting).
MEDIA_ROUTE_PREFIX = "/api/interactions/media/"

# Loopback hosts for which an http (non-TLS) served-media base URL is admitted —
# every other host must be https. ``InteractionsSettings.public_base_url``'s
# validator imports this set: an http served reference validated here is minted
# from that base, so the two admit exactly the same hosts.
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_DNS_LABEL = re.compile(r"[A-Za-z0-9-]{1,63}")

# A stored-media id: the urlsafe-base64 (no padding) of 32 random bytes, 43 chars.
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{43}")


def _is_media_route_url(value: str) -> bool:
    # A same-origin reference to media the skeleton serves: the fixed route prefix
    # followed by a well-formed 43-char stored-media id and nothing else.
    if not value.startswith(MEDIA_ROUTE_PREFIX):
        return False
    return _MEDIA_ID.fullmatch(value[len(MEDIA_ROUTE_PREFIX) :]) is not None


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


def _is_served_media_url(value: str) -> bool:
    # An absolute served-media reference: a well-formed absolute http(s) URL (host,
    # port and userinfo judged by ``_is_absolute_web_url``) whose path is exactly
    # ``MEDIA_ROUTE_PREFIX`` + a 43-char stored-media id, with no query or fragment.
    # http is admitted only when the host is a loopback host (``LOCAL_HTTP_HOSTS``),
    # because such a base is minted from ``InteractionsSettings.public_base_url``,
    # whose validator restricts http to those same hosts; https keeps any valid host.
    if not _is_absolute_web_url(value, schemes=("http", "https")):
        return False
    split = urlsplit(value)
    if split.scheme == "http" and split.hostname not in LOCAL_HTTP_HOSTS:
        return False
    if split.query or split.fragment or not split.path.startswith(MEDIA_ROUTE_PREFIX):
        return False
    return _MEDIA_ID.fullmatch(split.path[len(MEDIA_ROUTE_PREFIX) :]) is not None


def served_media_id(url: str) -> str | None:
    """The stored-media id a served ``image`` url references, or ``None`` if the url
    is not a served reference. Handles BOTH forms a request may carry: the
    same-origin relative ``{MEDIA_ROUTE_PREFIX}{id}`` an inbox ask stores, and the
    absolute ``http(s)`` served reference a channel send mints from
    ``public_base_url``. The prefix is located by parsing (the relative-form
    ``startswith``, the absolute-form url ``path``), never by substring search — so a
    prefix buried in a query or fragment is not mistaken for a served id."""
    if _is_media_route_url(url):
        return url[len(MEDIA_ROUTE_PREFIX) :]
    if _is_served_media_url(url):
        return urlsplit(url).path[len(MEDIA_ROUTE_PREFIX) :]
    return None


class MediaItem(BaseModel):
    """One media item shown WITH a message — a display element, and inbound the shape a guest's
    sent media takes.

    ``kind`` selects how it renders: an ``image`` inline, a ``document``/``video``/``audio`` as
    the matching file bubble, a ``link`` as a labelled anchor. ``url`` is the source. A file
    kind (``image``/``document``/``video``/``audio``) must be an absolute ``https`` URL, a
    same-origin ``{MEDIA_ROUTE_PREFIX}{id}`` reference to media the skeleton serves by id, or an
    absolute ``http(s)`` served reference of that same ``{MEDIA_ROUTE_PREFIX}{id}`` path a
    channel send mints from ``public_base_url`` (remote file media is https-only: the inbox CSP
    ``img-src`` admits ``https:``/``data:`` and same-origin but not ``http:``, so an ``http:``
    remote source would be an unrenderable record); ``image`` ADDITIONALLY admits an inline
    ``data:image/*`` URI — that inline form is image-only, a ``data:`` URI on any other file
    kind is refused. A ``link`` must be an absolute ``http(s)`` URL (anchors are not governed by
    ``img-src``; the human clicks through). A remote url names a host directly — an ASCII DNS
    name, dotted-quad IPv4, or bracketed IPv6 (IDN callers supply punycode); an embedded
    ``user@`` credential form is rejected as it spoofs the authority — and is always a single
    line — raw whitespace and control/format characters are rejected. ``caption`` is the
    accessibility text — the image's alt text, a file's label, or the link's display label.
    ``filename`` is the document's suggested display name; it is meaningful ONLY for a
    ``document`` item and is refused on every other kind.
    """

    kind: MediaKind
    url: str
    caption: str | None = None
    filename: str | None = None

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

    @field_validator("filename")
    @classmethod
    def _check_filename(cls, value: str | None) -> str | None:
        # Shape only; the kind coupling (document-only) is decided in :meth:`_check_filename_kind`
        # once ``kind`` is bound. A single-line non-blank label, capped — never a path, so raw
        # whitespace/control characters (an embedded newline, a bidi spoof) are refused.
        if value is not None:
            if not value.strip():
                raise ValueError("media filename must be non-blank when present")
            if len(value) > MEDIA_FILENAME_MAX_CHARS:
                raise ValueError(
                    f"media filename must be at most {MEDIA_FILENAME_MAX_CHARS} characters, got {len(value)}"
                )
            if any((ch.isspace() and ch != " ") or not ch.isprintable() for ch in value):
                raise ValueError("media filename must be a single-line label with no control characters")
        return value

    @model_validator(mode="after")
    def _check_filename_kind(self) -> MediaItem:
        # A filename names the download the medium offers, which only a ``document`` has; a
        # filename on any other kind is a caller bug, refused rather than silently ignored.
        if self.filename is not None and self.kind is not MediaKind.DOCUMENT:
            raise ValueError(f"filename is meaningful only for document media, not {self.kind.value}")
        return self

    @model_validator(mode="after")
    def _check_url_for_kind(self) -> MediaItem:
        if self.kind is MediaKind.LINK:
            if not _is_absolute_web_url(self.url, schemes=("http", "https")):
                raise ValueError("link media url must be an absolute http(s) URL")
            if len(self.url) > MEDIA_URL_MAX_CHARS:
                raise ValueError(
                    f"link media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(self.url)}"
                )
            return self
        # File media (image/document/video/audio): a same-origin served-media reference, an
        # absolute served-media url, or an absolute https url. Only ``image`` additionally admits
        # an inline ``data:image/*`` URI.
        kind = self.kind.value
        if self.url.startswith(_DATA_IMAGE_PREFIX):
            if self.kind is not MediaKind.IMAGE:
                raise ValueError(f"only image media may carry a data:image/* URI, not {kind}")
            if len(self.url) > MEDIA_DATA_URI_MAX_CHARS:
                raise ValueError(
                    f"image media data: URI must be at most {MEDIA_DATA_URI_MAX_CHARS} characters, got {len(self.url)}"
                )
        elif _is_media_route_url(self.url):
            # A same-origin reference to media the skeleton serves by id; the id
            # charset+length is the whole check (no host, relative path).
            pass
        elif _is_served_media_url(self.url):
            # An absolute served reference a channel send mints from public_base_url;
            # http is allowed only here (that base is loopback-restricted at settings).
            if len(self.url) > MEDIA_URL_MAX_CHARS:
                raise ValueError(
                    f"{kind} media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(self.url)}"
                )
        elif _is_absolute_web_url(self.url, schemes=("https",)):
            if len(self.url) > MEDIA_URL_MAX_CHARS:
                raise ValueError(
                    f"{kind} media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(self.url)}"
                )
        elif self.kind is MediaKind.IMAGE:
            raise ValueError("image media url must be an absolute https URL or a data:image/* URI")
        else:
            raise ValueError(f"{kind} media url must be an absolute https URL or a served-media reference")
        return self


def check_media_list(items: Sequence[MediaItem]) -> None:
    """List-level media caps every door that accepts media shares: a present media
    list is non-empty, holds at most ``MEDIA_MAX_ITEMS`` items, and its summed url text
    is within ``MEDIA_TOTAL_URI_CHARS``. Raises ``ValueError`` loudly; per-item shape is
    ``MediaItem``'s own concern. Callers run this on the RAW validated items before any
    store write, so an over-cap ask/notify is refused before a substitution stores bytes."""
    if not items:
        raise ValueError("media must be a non-empty list when present")
    if len(items) > MEDIA_MAX_ITEMS:
        raise ValueError(f"media carries at most {MEDIA_MAX_ITEMS} items, got {len(items)}")
    total = sum(len(item.url) for item in items)
    if total > MEDIA_TOTAL_URI_CHARS:
        raise ValueError(f"media total url length must be at most {MEDIA_TOTAL_URI_CHARS} characters, got {total}")


def validate_action_url(value: str) -> str:
    """Validate a tappable link-action URL — the ``url`` a link button/anchor opens when the
    human taps it. An absolute ``http(s)`` URL, single-line (raw whitespace and control/format
    characters rejected, as on a media url), within ``MEDIA_URL_MAX_CHARS``. Returns the value
    unchanged or raises ``ValueError``. Shared by every link-action option shape."""
    if not value.strip():
        raise ValueError("link url must be non-blank")
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ValueError("link url must be a single-line URL with no whitespace or control characters")
    if not _is_absolute_web_url(value, schemes=("http", "https")):
        raise ValueError("link url must be an absolute http(s) URL")
    if len(value) > MEDIA_URL_MAX_CHARS:
        raise ValueError(f"link url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(value)}")
    return value


class LocationElement(BaseModel):
    """A geographic point shared on a message — the one shape used BOTH ways: an outbound place a
    flow shares and the inbound location a guest sent.

    ``latitude``/``longitude`` are WGS84 decimal degrees, bounded to their valid ranges
    (latitude -90..90, longitude -180..180). ``name`` is an optional place label and ``address``
    an optional street address, each a single-line non-blank string within its cap when present
    (raw whitespace and control/format characters are rejected, as on a media url — an embedded
    newline or bidi spoof would corrupt the rendered pin label). A channel that cannot render a
    map renders the coordinates (and any name/address) as text. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None

    @field_validator("latitude")
    @classmethod
    def _check_latitude(cls, value: float) -> float:
        if not -90.0 <= value <= 90.0:
            raise ValueError(f"latitude must be within -90..90 degrees, got {value}")
        return value

    @field_validator("longitude")
    @classmethod
    def _check_longitude(cls, value: float) -> float:
        if not -180.0 <= value <= 180.0:
            raise ValueError(f"longitude must be within -180..180 degrees, got {value}")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("location name must be non-blank when present")
            if len(value) > LOCATION_NAME_MAX_CHARS:
                raise ValueError(
                    f"location name must be at most {LOCATION_NAME_MAX_CHARS} characters, got {len(value)}"
                )
            if any((ch.isspace() and ch != " ") or not ch.isprintable() for ch in value):
                raise ValueError("location name must be a single-line label with no control characters")
        return value

    @field_validator("address")
    @classmethod
    def _check_address(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("location address must be non-blank when present")
            if len(value) > LOCATION_ADDRESS_MAX_CHARS:
                raise ValueError(
                    f"location address must be at most {LOCATION_ADDRESS_MAX_CHARS} characters, got {len(value)}"
                )
            if any((ch.isspace() and ch != " ") or not ch.isprintable() for ch in value):
                raise ValueError("location address must be a single-line label with no control characters")
        return value


_SCALAR_FORM_TYPES = ("string", "boolean", "integer", "number")


class FormOption(BaseModel):
    """One per-send choice for a form field: ``value`` is the string submitted as
    the answer, ``label`` (when set) is shown to the human in its place. A per-send
    option list REPLACES a property's schema ``enum`` for ONE send — the published
    form is unchanged, so a variant needs no re-publish. Frozen."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str | None = None

    @field_validator("value")
    @classmethod
    def _value_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form option value must be non-blank")
        return value

    @field_validator("label")
    @classmethod
    def _label_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("form option label must be non-blank when present")
        return value


class FormData(BaseModel):
    """Per-send data layered over a form's published schema for ONE send.

    ``values`` prefills top-level properties — each entry keyed by property name,
    its value shown filled in and validated against that property's schema.
    ``options`` supplies a per-send choice list for a property whose schema is a
    string (or an array of strings), keyed by property name: the list REPLACES that
    property's ``enum`` for this send only (labels shown, values submitted). The
    model holds only the shape; the cross-check against the schema (unknown
    property, a value that fails its schema, options on a non-string property, an
    empty list) is done once by the interaction request. Frozen."""

    model_config = ConfigDict(frozen=True)

    values: dict[str, Any] = {}
    options: dict[str, list[FormOption]] = {}


class FormPage(BaseModel):
    """One step of a stepped form: ``title`` heads the step and ``fields`` names the
    top-level properties shown on it. Across a form's ``pages`` every property
    appears exactly once (the interaction request enforces the coverage); absent
    ``pages`` means one page. Frozen."""

    model_config = ConfigDict(frozen=True)

    title: str
    fields: list[str]

    @field_validator("title")
    @classmethod
    def _title_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form page title must be non-blank")
        return value

    @field_validator("fields")
    @classmethod
    def _fields_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("form page fields must be a non-empty list")
        return value


def _schema_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # The form's top-level ``properties`` as a typed map of name -> property schema.
    # An absent / non-object ``properties`` yields an empty map; a property whose own
    # schema is not an object is dropped (a value/option keyed to it reads as unknown).
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, prop in cast("dict[Any, Any]", properties).items():
        if isinstance(prop, dict):
            result[str(name)] = cast("dict[str, Any]", prop)
    return result


def _form_option_values(prop: dict[str, Any], options: list[FormOption] | None) -> list[str] | None:
    # The allowed string set for a prefilled value: the per-send option values when a
    # per-send list is given (it replaces the enum for this send), else the property's
    # own ``enum`` — or None when the property constrains nothing.
    if options is not None:
        return [option.value for option in options]
    enum = prop.get("enum")
    if isinstance(enum, list):
        return [str(choice) for choice in cast("list[Any]", enum)]
    return None


def _form_property_is_stringish(prop: dict[str, Any]) -> bool:
    # A property a per-send option list may target: a string, or an array whose items
    # are strings. Any other property carries choices no single control can render.
    if prop.get("type") == "string":
        return True
    items = prop.get("items")
    return (
        prop.get("type") == "array"
        and isinstance(items, dict)
        and cast("dict[str, Any]", items).get("type") == "string"
    )


def _check_form_value(name: str, prop: dict[str, Any], value: Any, options: list[FormOption] | None) -> None:
    # Validate one prefilled ``value`` against its property's scalar schema (plus any
    # enum / per-send option constraint). A property without a renderable scalar (or
    # array-of-strings) type cannot be shown filled in, so it raises rather than
    # storing an unrenderable prefill. Raises ``ValueError`` naming the field.
    ptype = prop.get("type")
    if ptype == "string":
        if not isinstance(value, str):
            raise ValueError(f"form data value for {name!r} must be a string")
        allowed = _form_option_values(prop, options)
        if allowed is not None and value not in allowed:
            raise ValueError(f"form data value for {name!r} must be one of {allowed}")
    elif ptype == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be a boolean")
    elif ptype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be an integer")
    elif ptype == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be a number")
    elif ptype == "array" and isinstance(prop.get("items"), dict):
        items = cast("dict[str, Any]", prop["items"])
        if items.get("type") != "string":
            raise ValueError(f"form data value for {name!r} must be a list of strings")
        if not isinstance(value, list):
            raise ValueError(f"form data value for {name!r} must be a list of strings")
        value_list = cast("list[Any]", value)
        if not all(isinstance(item, str) for item in value_list):
            raise ValueError(f"form data value for {name!r} must be a list of strings")
        allowed = _form_option_values(items, options)
        if allowed is not None:
            bad = [item for item in value_list if item not in allowed]
            if bad:
                raise ValueError(f"form data value for {name!r} contains choices outside the allowed set: {bad}")
    else:
        raise ValueError(
            f"form data cannot prefill {name!r}: its schema type {ptype!r} is not a renderable scalar "
            f"({', '.join(_SCALAR_FORM_TYPES)}) or an array of strings"
        )


def check_form_data(schema: dict[str, Any], data: FormData) -> None:
    """Validate a form's per-send :class:`FormData` against its schema: every
    ``values`` / ``options`` key is a declared top-level property, each prefilled
    value fits its property's schema, and a per-send option list targets only a
    string (or array-of-strings) property and is non-empty. Raises ``ValueError``
    naming the offending field."""
    props = _schema_properties(schema)
    for name, option_list in data.options.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"form data options names unknown property {name!r}")
        if not _form_property_is_stringish(prop):
            raise ValueError(f"form data options for {name!r} require a string (or array-of-strings) property")
        if not option_list:
            raise ValueError(f"form data options for {name!r} must be a non-empty list")
    for name, value in data.values.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"form data values names unknown property {name!r}")
        _check_form_value(name, prop, value, data.options.get(name))


def check_form_pages(schema: dict[str, Any], pages: list[FormPage]) -> None:
    """Validate a form's ``pages`` against its schema: every top-level property
    appears exactly once across the pages, and every named field is a declared
    property. Raises ``ValueError`` naming the missing / duplicate / unknown
    field."""
    declared = list(_schema_properties(schema))
    seen: list[str] = []
    for page in pages:
        for field in page.fields:
            if field not in declared:
                raise ValueError(f"form page {page.title!r} names unknown property {field!r}")
            if field in seen:
                raise ValueError(f"form page property {field!r} appears on more than one page")
            seen.append(field)
    missing = [name for name in declared if name not in seen]
    if missing:
        raise ValueError(f"form pages omit properties: {missing}")


class InteractionRequest(BaseModel):
    """The durable question. One per stream entry."""

    interaction_id: str
    group_id: str
    question: str
    answer_format: AnswerFormat = AnswerFormat.TEXT
    format_payload: dict[str, Any] | None = None
    # What a channel-delivered ask does with a guest reply the answer door REJECTS: ``retry``
    # (default — keep the ask parked and tell the guest what's expected) or ``bridge`` (treat an
    # unmatched reply as a digression — keep the ask parked with no notice and hand the reply to
    # the conversation as a fresh routed turn). Set per ask by the tool author; the default is a
    # zero-behavior-change for every existing ask.
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY
    # A per-ask custom guest-facing rejection notice, used ONLY under the ``retry`` policy: when
    # set it REPLACES the platform's built-in retry notice. A literal ``{reason}`` token (if
    # present) is filled with the door's rejection reason by a PLAIN substitution — a notice
    # without the token is sent verbatim, and stray braces never raise (never ``str.format``). It
    # customizes the CORE-sent notice only; a channel that owns its correction surface renders its
    # own text off the door's reason and ignores this. Under the ``bridge`` policy it is IGNORED (a
    # digression never notifies). ``None`` uses the built-in default.
    mismatch_notice: str | None = None
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
    # Display-only media shown WITH the question: images and links the human sees when
    # reading it. It never becomes part of the answer. It renders in the inbox AND, on a
    # channel-delivered ask, is forwarded on ``ChannelDelivery.media`` to the channel plugin
    # (a data:image served reference is absolute on that path so a vendor can fetch it
    # off-origin). None means no media; a present list is non-empty (a present-but-empty list
    # is a caller bug). Set per question by the tool author.
    media: list[MediaItem] | None = None
    # Wait discipline. ``sync`` blocks the asking caller until the answer or the
    # timeout; ``async`` PARKS the caller — the ask returns a SuspendedInteraction
    # and a later answer/expiry resumes work by invoking ``continuation_tool`` as
    # ``continuation_identity``. Both continuation fields are required iff async
    # and forbidden iff sync.
    mode: Literal["sync", "async"] = "sync"
    # async only: the registered tool NAME run when the answer arrives or the
    # question expires — resolved through the platform tool registry, carrying no
    # resuming-driver state.
    continuation_tool: str | None = None
    # async only: the execution key (an api-key ``user_id``) the continuation runs
    # AS — rebound at answer time, never the answerer's identity. Same string
    # representation as ``execution_key`` elsewhere in the contract.
    continuation_identity: str | None = None
    # The ambient state context the ORIGINAL turn deposited, carried verbatim across
    # the park so the resumed run's state writes complete their provenance from the
    # same door — one generic snapshot (a later resume attribution joins the same
    # field). None when the park ran under no state context.
    continuation_state_context: StateContext | None = None
    # When the parked question expires. Distinct from ``timeout_at`` (the sync
    # wait budget) and mutually exclusive with a sync ``timeout`` at the ask
    # surface (see ``check_ask_timing``). Required for async (a park always carries
    # a deadline; the model validator rejects an async request without it); None
    # only on a sync question.
    expiry_at: datetime | None = None

    @field_validator("question")
    @classmethod
    def _check_question(cls, value: str) -> str:
        if len(value) > QUESTION_MAX_CHARS:
            raise ValueError(f"question must be at most {QUESTION_MAX_CHARS} characters, got {len(value)}")
        return value

    @field_validator("mismatch_notice")
    @classmethod
    def _check_mismatch_notice(cls, value: str | None) -> str | None:
        # None uses the built-in default; a set notice is non-blank and within the guest-reply cap.
        if value is not None:
            if not value.strip():
                raise ValueError("mismatch_notice must be non-blank when set")
            if len(value) > MISMATCH_NOTICE_MAX_CHARS:
                raise ValueError(
                    f"mismatch_notice must be at most {MISMATCH_NOTICE_MAX_CHARS} characters, got {len(value)}"
                )
        return value

    @field_validator("media")
    @classmethod
    def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        if value is not None:
            check_media_list(value)
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

    @field_validator("expiry_at")
    @classmethod
    def _ensure_expiry_tz_aware(cls, value: datetime | None) -> datetime | None:
        # The async park deadline is compared against an aware ``now()`` by the
        # expiry reaper; a naive value would raise TypeError there. None stays None
        # (a sync question carries no deadline); a set value is reject-naive +
        # UTC-normalized, the same strictness as its ``created_at``/``timeout_at``
        # siblings.
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_payload(self) -> InteractionRequest:
        if self.answer_format is AnswerFormat.SELECT:
            payload = self.format_payload or {}
            options = payload.get("options")
            if not options:
                raise ValueError("select answer_format requires non-empty options")
            # SELECT carries only its answer set; form-only keys (data/pages) and any
            # other extra are a caller bug, refused rather than silently ignored.
            extra = set(payload) - {"options"}
            if extra:
                raise ValueError(f"select answer_format payload carries only options, got extra {sorted(extra)}")
        elif self.answer_format is AnswerFormat.FORM:
            payload = self.format_payload or {}
            schema = payload.get("schema")
            if not schema:
                raise ValueError("form answer_format requires a schema")
            # Per-send prefill/options and stepped pages are validated ONCE here, the
            # single seam every ask door flows through, against the form's own schema.
            data = payload.get("data")
            pages = payload.get("pages")
            if (data is not None or pages is not None) and not isinstance(schema, dict):
                raise ValueError("form data/pages require an object schema")
            if data is not None:
                check_form_data(cast("dict[str, Any]", schema), FormData.model_validate(data))
            if pages is not None:
                check_form_pages(
                    cast("dict[str, Any]", schema), [FormPage.model_validate(page) for page in cast("list[Any]", pages)]
                )
        elif self.answer_format is AnswerFormat.EXTERNAL:
            url = (self.format_payload or {}).get("url")
            # The external surface is reached through this url; a non-str or empty
            # value would leave the caller with no place to send the human.
            if not isinstance(url, str) or not url:
                raise ValueError("external answer_format requires a non-empty string url in format_payload")
        elif self.answer_format is AnswerFormat.TEXT:
            # TEXT carries no payload EXCEPT an OPTIONAL ``options`` list of suggested
            # replies: a tapped option submits its own text as the free-text answer, which
            # stays unconstrained (unlike SELECT, where options ARE the answer set). Any
            # other key on a text payload is a caller bug.
            payload = self.format_payload
            if payload is not None:
                extra = set(payload) - {"options"}
                if extra:
                    raise ValueError(
                        f"text answer_format payload carries only optional options, got extra {sorted(extra)}"
                    )
                options = payload.get("options")
                if options is not None and not options:
                    raise ValueError("text answer_format options must be a non-empty list when present")
        else:
            if self.format_payload is not None:
                raise ValueError(f"{self.answer_format.value} answer_format carries no format_payload")
        return self

    @model_validator(mode="after")
    def _check_continuation(self) -> InteractionRequest:
        if self.mode == "async":
            if self.continuation_tool is None:
                raise ValueError("async mode requires continuation_tool")
            if self.continuation_identity is None:
                raise ValueError("async mode requires continuation_identity")
            if self.expiry_at is None:
                # Without an ``expiry_at`` an async park is never expiry-indexed, so
                # the reaper can never fire its continuation — the idle TTL would drop
                # it silently. Require the deadline rather than persist an
                # unresumable park.
                raise ValueError("async mode requires expiry_at")
        else:
            if self.continuation_tool is not None:
                raise ValueError("sync mode carries no continuation_tool")
            if self.continuation_identity is not None:
                raise ValueError("sync mode carries no continuation_identity")
        return self


class InteractionResponse(BaseModel):
    """The answer. Validated server-side before it wakes the caller.

    ``params`` is the OPTIONAL opaque channel enrichment carried WITH the answer — the answer-path
    counterpart of the bridge path's :class:`~tai42_contract.channels.InboundBridge.params` and a
    conversation entry's ``params``: when a tap/reply that ANSWERS a pending ask also carries
    channel-specific context (a tapped reply id, a template button payload, a referral, the
    reply-to context the guest quoted) the channel encodes it as string entries here so the asking
    flow reads them beside ``answer``. The SAME transport vocabulary
    (:func:`~tai42_contract.entry_params.validate_entry_params`) bounds it as every params seam;
    the platform attaches no meaning and NO TRUST. ``None`` means no enrichment — a plain answer,
    byte-identical to the pre-params envelope.
    """

    interaction_id: str
    answer: Any
    answered_by: str
    answered_at: datetime
    params: dict[str, str] | None = None

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return validate_entry_params(value)


class InteractionState(BaseModel):
    """Mutable companion to the immutable stream entry, keyed by interaction id."""

    status: Literal["pending", "answered"]
    group_id: str
    request: InteractionRequest
    response: InteractionResponse | None = None


class SuspendedInteraction(BaseModel):
    """The sentinel an ``async`` ask returns in place of an answer.

    Any async-suspending tool returns it to signal the caller was parked, keyed by
    ``interaction_id`` so the resume path can find the parked question. Generic:
    a resuming driver's resume state is keyed by this id, not carried here.
    """

    interaction_id: str
    expiry_at: datetime | None = None
    # The resume continuation this park was raised UNDER — the one driver entitled to ADOPT
    # it as its own park (see ``assert_park_adoptable``). A park has exactly one resume owner,
    # so a caller that turns a returned sentinel into its own park state must be that owner.
    # An ask that parks always stamps it, so ``None`` means the sentinel was NOT minted by an
    # ask: a nested RUN's park surfaced at its tool face, which no caller may adopt.
    resume_owner: str | None = None

    @field_validator("expiry_at")
    @classmethod
    def _ensure_expiry_tz_aware(cls, value: datetime | None) -> datetime | None:
        # The async park deadline is compared against an aware ``now()`` by the
        # expiry reaper; a naive value would raise TypeError there. None stays None
        # (a sync question carries no deadline); a set value is reject-naive +
        # UTC-normalized, the same strictness as its ``InteractionRequest`` sibling.
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)
