"""Display-media models and their url discipline.

``MediaItem`` is one media element shown with a message (image/document/video/audio/link);
``check_media_list`` bounds a list of them; ``validate_action_url`` validates a tappable
link-action URL; ``served_media_id`` extracts a served-media reference's stored id.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator, model_validator


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


def _validate_link_media_url(url: str) -> None:
    # A ``link`` (a labelled anchor the human clicks through): an absolute http(s) URL
    # within the single-url cap. Anchors are not governed by the inbox ``img-src``.
    if not _is_absolute_web_url(url, schemes=("http", "https")):
        raise ValueError("link media url must be an absolute http(s) URL")
    if len(url) > MEDIA_URL_MAX_CHARS:
        raise ValueError(f"link media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(url)}")


def _validate_file_media_url(url: str, kind: MediaKind) -> None:
    # File media (image/document/video/audio): a same-origin served-media reference, an
    # absolute served-media url, or an absolute https url. Only ``image`` additionally admits
    # an inline ``data:image/*`` URI.
    kind_name = kind.value
    if url.startswith(_DATA_IMAGE_PREFIX):
        if kind is not MediaKind.IMAGE:
            raise ValueError(f"only image media may carry a data:image/* URI, not {kind_name}")
        if len(url) > MEDIA_DATA_URI_MAX_CHARS:
            raise ValueError(
                f"image media data: URI must be at most {MEDIA_DATA_URI_MAX_CHARS} characters, got {len(url)}"
            )
    elif _is_media_route_url(url):
        # A same-origin reference to media the skeleton serves by id; the id
        # charset+length is the whole check (no host, relative path).
        pass
    elif _is_served_media_url(url):
        # An absolute served reference a channel send mints from public_base_url;
        # http is allowed only here (that base is loopback-restricted at settings).
        if len(url) > MEDIA_URL_MAX_CHARS:
            raise ValueError(f"{kind_name} media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(url)}")
    elif _is_absolute_web_url(url, schemes=("https",)):
        if len(url) > MEDIA_URL_MAX_CHARS:
            raise ValueError(f"{kind_name} media url must be at most {MEDIA_URL_MAX_CHARS} characters, got {len(url)}")
    elif kind is MediaKind.IMAGE:
        raise ValueError("image media url must be an absolute https URL or a data:image/* URI")
    else:
        raise ValueError(f"{kind_name} media url must be an absolute https URL or a served-media reference")


class MediaItem(BaseModel):
    """One media item shown WITH a message — a display element, and inbound the shape a participant's
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
            _validate_link_media_url(self.url)
        else:
            _validate_file_media_url(self.url, self.kind)
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
