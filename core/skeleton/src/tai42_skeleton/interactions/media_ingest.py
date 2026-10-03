"""The served-media ingestion chokepoint — ``ingest_media`` and the two-phase ``bind_media``.

ONE seam every byte-ingest door flows through (the channel adapters, the web upload door). It
caps the read, sniffs the real type and requires its top-level type to agree with the declared one,
enforces the per-kind allowlist against the authoritative sniffed type, sanitises the display
filename, writes the metadata record FIRST and then the blob (record-first,
so a crash leaves a reaper-reachable record, never an untracked orphan), and mints a served
reference. Every failure raises one of the typed :class:`MediaIngestError` subclasses — never a
silent skip.
"""

from __future__ import annotations

import hashlib
import mimetypes
import secrets
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast

import filetype
from tai42_contract.app import tai42_app
from tai42_contract.conversations import InboundMediaKind, InboundRejectionReason
from tai42_contract.interactions import (
    MEDIA_FILENAME_MAX_CHARS,
    MEDIA_ROUTE_PREFIX,
    IngestedMedia,
    MediaItem,
    MediaKind,
    MediaOrigin,
)
from tai42_contract.interactions.models.media_errors import (
    MediaAlreadyBoundError,
    MediaIngestError,
    MediaNotFoundError,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.interactions import MediaIngestCapSettings, media_ingest_cap_settings

from tai42_skeleton.channels.inbound import emit_inbound_media_ingested, emit_inbound_media_rejected
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore, inbound_media_blob_path
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.settings.media_ingest import MediaIngestSettings, media_ingest_settings

if TYPE_CHECKING:
    from tai42_skeleton.app.facets import StorageFacet

# The signature window ``filetype`` reads (``filetype/utils.py:10`` ``_NUM_SIGNATURE_BYTES``); the
# in-seam ISOBMFF matcher bounds its ``ftyp`` box to this window too.
_SNIFF_WINDOW = 8192

# The ISO base media file format brands the in-seam matcher maps to a MIME (first match wins),
# filling the gap ``filetype`` 1.2.0 leaves — its ``M3gp`` matcher compares ``b'ftyp3gp'`` at
# offset 0 while the ``ftyp`` box sits at offset 4, so a real 3gp sniffs ``None``. ``3gp*``/``3g2*``
# match any 4th byte; ``M4V ``/``M4A ``/``qt  `` carry a trailing space to a full 4 bytes.
_ISOBMFF_EXACT_BRAND_MIMES: dict[bytes, str] = {
    b"mp41": "video/mp4",
    b"mp42": "video/mp4",
    b"isom": "video/mp4",
    b"avc1": "video/mp4",
    b"M4V ": "video/mp4",
    b"M4A ": "audio/mp4",
    b"qt  ": "video/quicktime",
}

# Every MIME the ISOBMFF brand matcher can return — the settings boot validator's second matcher set.
ISOBMFF_BRAND_MIMES: frozenset[str] = frozenset({"video/3gpp", "video/3gpp2", *_ISOBMFF_EXACT_BRAND_MIMES.values()})

# Active/renderable content is never served inline and never on any allowlist; the seam asserts it
# is absent defensively, so a misconfigured allowlist that admitted one still fails loud at ingest.
_ACTIVE_CONTENT_MIMES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/javascript",
        "application/javascript",
        "application/xml",
    }
)


def _brand_mime(brand: bytes) -> str | None:
    # One ISOBMFF brand -> a MIME, or None. A trailing compatible brand may be < 4 bytes.
    if len(brand) < 4:
        return None
    if brand[:3] == b"3gp":
        return "video/3gpp"
    if brand[:3] == b"3g2":
        return "video/3gpp2"
    return _ISOBMFF_EXACT_BRAND_MIMES.get(brand)


def _sniff_isobmff(buffer: bytes) -> str | None:
    """The MIME an ISO base media file format ``ftyp`` box maps to via its brands, or ``None``.

    Reads the box size at offset 0, the ``ftyp`` type at offset 4, the major brand at offset 8, and
    the compatible brands from offset 16 (ISO/IEC 14496-12, mirroring ``filetype``'s own
    ``IsoBmff._get_ftyp``). Returns ``None`` when the buffer is not an ``ftyp`` box or the declared
    box size is unusable — ``< 16``, larger than the buffer, or larger than the sniff window — so a
    crafted 4 GB box-size field can never drive an unbounded compatible-brand loop.
    """
    if len(buffer) < 16 or buffer[4:8] != b"ftyp":
        return None
    ftyp_len = int.from_bytes(buffer[0:4], "big")
    if ftyp_len < 16 or ftyp_len > len(buffer) or ftyp_len > _SNIFF_WINDOW:
        return None
    brands = [buffer[8:12]]
    brands.extend(buffer[i : i + 4] for i in range(16, min(ftyp_len, len(buffer)), 4))
    for brand in brands:
        mime = _brand_mime(brand)
        if mime is not None:
            return mime
    return None


def _normalize_mime(declared_mime: str | None) -> str | None:
    # The declared content-type without its parameters, lower-cased and stripped — or None.
    if declared_mime is None:
        return None
    bare = declared_mime.split(";", 1)[0].strip().lower()
    return bare or None


def _kind_from_mime(mime: str) -> MediaKind:
    # The media kind an effective MIME derives to (``MediaKind`` has no ``FILE`` member).
    top = mime.split("/", 1)[0]
    if top == "image":
        return MediaKind.IMAGE
    if top == "video":
        return MediaKind.VIDEO
    if top == "audio":
        return MediaKind.AUDIO
    return MediaKind.DOCUMENT


def _kind_hint_media_kind(kind_hint: InboundMediaKind | str | None) -> MediaKind | None:
    # The pre-read cap's kind: a hint that maps cleanly to a byte-carrying media kind, else None
    # (worst case). Sticker/file and anything unrecognised fall back to the largest cap.
    if kind_hint is None:
        return None
    value = kind_hint.value if isinstance(kind_hint, InboundMediaKind) else str(kind_hint)
    return {
        "image": MediaKind.IMAGE,
        "video": MediaKind.VIDEO,
        "audio": MediaKind.AUDIO,
        "document": MediaKind.DOCUMENT,
    }.get(value)


def _kind_hint_str(kind_hint: InboundMediaKind | str | None) -> str:
    # The generic kind label the rejection telemetry carries (never participant content).
    if kind_hint is None:
        return ""
    return kind_hint.value if isinstance(kind_hint, InboundMediaKind) else str(kind_hint)


def _truncate_keeping_extension(name: str) -> str:
    # Truncate to ``MEDIA_FILENAME_MAX_CHARS`` preserving a real extension (after the LAST dot,
    # non-empty, dot-free, <= 16 chars); with no usable extension, truncate the whole label.
    if len(name) <= MEDIA_FILENAME_MAX_CHARS:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and ext and "." not in ext and len(ext) <= 16:
        return f"{stem[: MEDIA_FILENAME_MAX_CHARS - len(ext) - 1]}.{ext}"
    return name[:MEDIA_FILENAME_MAX_CHARS]


def _sanitize_filename(raw: str | None, *, kind: MediaKind, mime: str) -> str:
    """A display filename that always satisfies ``MediaItem._check_filename`` (non-blank, single-line, capped).

    Strips leading/trailing whitespace, deletes every control/non-printable character and every
    whitespace character other than a plain space, truncates preserving the extension, and — when
    the result is blank or the input absent — derives a generic name from the kind + the MIME's
    canonical extension. NEVER raises: a ``ValidationError`` from a ``MediaItem`` built off its
    output is a programming error (a sanitiser bug), not caller input.
    """
    stripped = "" if raw is None else raw.strip()
    kept = "".join(ch for ch in stripped if ch.isprintable() and not (ch.isspace() and ch != " "))
    if not kept:
        return f"{kind.value}{mimetypes.guess_extension(mime) or ''}"
    return _truncate_keeping_extension(kept)


def _classify(buffer: bytes, declared_mime: str | None, settings: MediaIngestSettings) -> tuple[MediaKind, str]:
    # The per-kind sniff policy: image/audio/video REQUIRE a positive sniff whose TOP-LEVEL type
    # agrees with the declared type; the SNIFFED type is authoritative — it is the effective_mime,
    # checked against the allowlist and stored/served as the item's mime. A document accepts a
    # positive sniff on the document allowlist, or a None sniff only for the text-like declared
    # types. Returns (kind, effective_mime) or raises MediaTypeNotAllowedError.
    guess = filetype.guess(buffer)
    sniffed_mime = guess.mime if guess is not None else None
    declared = _normalize_mime(declared_mime)
    if sniffed_mime is None and declared in ISOBMFF_BRAND_MIMES:
        sniffed_mime = _sniff_isobmff(buffer)

    if sniffed_mime is not None:
        if declared is not None and declared.split("/", 1)[0] != sniffed_mime.split("/", 1)[0]:
            raise MediaTypeNotAllowedError(
                f"declared media type {declared!r} disagrees with the sniffed type {sniffed_mime!r}"
            )
        kind = _kind_from_mime(sniffed_mime)
        effective_mime = sniffed_mime
    else:
        # No positive sniff: only a text-like document declared on the None-sniff allowlist is admitted.
        if declared is None or declared not in settings.document_none_sniff_mime_allowlist:
            raise MediaTypeNotAllowedError(f"media of declared type {declared!r} did not sniff to an allowed type")
        kind = MediaKind.DOCUMENT
        effective_mime = declared

    if effective_mime in _ACTIVE_CONTENT_MIMES:
        raise MediaTypeNotAllowedError(f"active content type {effective_mime!r} is never served inline")
    if effective_mime not in settings.allowlist_for(kind):
        raise MediaTypeNotAllowedError(f"media type {effective_mime!r} is not on the {kind.value} allowlist")
    return kind, effective_mime


async def _read_capped(
    source: AsyncIterator[bytes],
    kind_hint: InboundMediaKind | str | None,
    declared_mime: str | None,
    settings: MediaIngestSettings,
    cap_settings: MediaIngestCapSettings,
) -> tuple[bytearray, MediaKind, str, str]:
    # Stream the source into one buffer under the per-kind cap, sniffing the kind EARLY. The pre-read
    # cap guards the running total until the signature window is buffered (or the source ends), then
    # the SNIFFED kind's cap guards the remainder. Returns (buffer, kind, effective_mime, sha256_hex).
    cap = cap_settings.cap_for(mk) if (mk := _kind_hint_media_kind(kind_hint)) is not None else cap_settings.max_cap
    buffer = bytearray()
    digest = hashlib.sha256()
    classified: tuple[MediaKind, str] | None = None

    async def _read() -> AsyncIterator[bytes]:
        # Translate a fault raised BY THE SOURCE (a vendor fetch / a torn upload) into the typed
        # MediaSourceReadError; the cap/sniff raises below happen on already-read bytes, outside this.
        try:
            async for chunk in source:
                yield chunk
        except Exception as exc:
            raise MediaSourceReadError(f"media source read failed: {type(exc).__name__}") from exc

    async for chunk in _read():
        buffer.extend(chunk)
        digest.update(chunk)
        if len(buffer) > cap:
            raise MediaTooLargeError(f"media exceeds the {cap}-byte cap")
        if classified is None and len(buffer) >= _SNIFF_WINDOW:
            classified = _classify(bytes(buffer), declared_mime, settings)
            cap = cap_settings.cap_for(classified[0])
            if len(buffer) > cap:
                raise MediaTooLargeError(f"media exceeds the {cap}-byte cap for {classified[0].value}")
    if classified is None:
        classified = _classify(bytes(buffer), declared_mime, settings)
        if len(buffer) > cap_settings.cap_for(classified[0]):
            raise MediaTooLargeError(
                f"media exceeds the {cap_settings.cap_for(classified[0])}-byte cap for {classified[0].value}"
            )
    kind, effective_mime = classified
    return buffer, kind, effective_mime, digest.hexdigest()


def _reason_for(exc: MediaIngestError) -> InboundRejectionReason:
    if isinstance(exc, MediaTooLargeError):
        return InboundRejectionReason.TOO_LARGE
    if isinstance(exc, MediaTypeNotAllowedError):
        return InboundRejectionReason.UNSUPPORTED_TYPE
    return InboundRejectionReason.COULD_NOT_RECEIVE


def _storage_provider():
    # The registered blob provider off the skeleton StorageFacet (the contract AppStorage Protocol
    # has no ``provider`` member), None while dead by default.
    return cast("StorageFacet", tai42_app.storage).provider


async def ingest_media(
    *,
    source: AsyncIterator[bytes],
    kind_hint: InboundMediaKind | str | None,
    declared_mime: str | None,
    filename: str | None,
    declared_size: int | None,
    origin: MediaOrigin,
) -> IngestedMedia:
    """The ONE ingestion chokepoint — cap, sniff, allowlist, record-first persist, served reference.

    The seam trusts its OWN computed content digest (returned as :attr:`IngestedMedia.sha256`).
    """
    settings = media_ingest_settings()
    cap_settings = media_ingest_cap_settings()
    try:
        cap = cap_settings.cap_for(mk) if (mk := _kind_hint_media_kind(kind_hint)) is not None else cap_settings.max_cap
        if declared_size is not None and declared_size > cap:
            raise MediaTooLargeError(f"declared media size {declared_size} exceeds the {cap}-byte cap")

        buffer, media_kind, effective_mime, sha256_hex = await _read_capped(
            source, kind_hint, declared_mime, settings, cap_settings
        )
        sanitized = _sanitize_filename(filename, kind=media_kind, mime=effective_mime)

        provider = _storage_provider()
        if provider is None:
            raise MediaStoreUnavailableError("no blob provider is registered — inbound media cannot be stored")

        media_id = secrets.token_urlsafe(32)
        storage_path = inbound_media_blob_path(media_id)
        conv_settings = ConversationsSettings()
        meta_store = InboundMediaMetaStore(conv_settings)
        pending = origin.message_id is None
        now = time.time()
        expiry_at = now + (settings.pending_ttl_seconds if pending else conv_settings.answer_retention_ttl_seconds)

        await meta_store.put(
            media_id,
            mime=effective_mime,
            size=len(buffer),
            sha256=sha256_hex,
            filename=sanitized,
            kind=media_kind,
            storage_path=storage_path,
            pending=pending,
            owner_channel_id=origin.channel_id,
            owner_participant_identity=origin.participant_identity,
            message_id=origin.message_id,
            expiry_at=expiry_at,
        )
        try:
            await provider.upload_bytes(storage_path, bytes(buffer), content_type=effective_mime)
        except Exception as exc:
            # The metadata record is LEFT for the reaper to reclaim at its expiry — the seam does no
            # best-effort cleanup (that would make it a second deleter; the reaper is the sole one).
            raise MediaStoreUnavailableError(f"blob store write failed: {type(exc).__name__}") from exc

        item = MediaItem(
            kind=media_kind,
            url=f"{MEDIA_ROUTE_PREFIX}{media_id}",
            caption=None,
            filename=sanitized if media_kind is MediaKind.DOCUMENT else None,
        )
        await emit_inbound_media_ingested(
            channel_id=origin.channel_id,
            participant_identity=origin.participant_identity,
            message_id=origin.message_id,
            media_id=media_id,
            kind=media_kind.value,
            size=len(buffer),
            sha256=sha256_hex,
            mime=effective_mime,
            pending=pending,
        )
        return IngestedMedia(
            item=item, media_id=media_id, size=len(buffer), sha256=sha256_hex, mime=effective_mime, pending=pending
        )
    except MediaIngestError as exc:
        await emit_inbound_media_rejected(
            channel_id=origin.channel_id,
            participant_identity=origin.participant_identity,
            message_id=origin.message_id,
            kind=_kind_hint_str(kind_hint),
            reason=_reason_for(exc),
        )
        raise


async def bind_media(media_id: str, *, origin: MediaOrigin) -> IngestedMedia:
    """Promote a PENDING ingested media to the owning record's retention horizon (the two-phase attach).

    ``origin.message_id`` is REQUIRED. Idempotent on a re-bind to the same message. Raises
    :class:`MediaNotFoundError` (unknown / expired / not-owned — one uniform error) or
    :class:`MediaAlreadyBoundError` (bound to a different message).
    """
    if origin.message_id is None:
        raise ValueError("bind_media requires origin.message_id")
    conv_settings = ConversationsSettings()
    meta_store = InboundMediaMetaStore(conv_settings)
    now = time.time()
    code = await meta_store.bind(
        media_id,
        message_id=origin.message_id,
        channel_id=origin.channel_id,
        participant_identity=origin.participant_identity,
        now=now,
        expiry_at=now + conv_settings.answer_retention_ttl_seconds,
    )
    if code == -1:
        raise MediaNotFoundError(f"no pending media {media_id!r} to bind")
    if code == -2:
        raise MediaAlreadyBoundError(f"media {media_id!r} is already bound to a different message")
    meta = await meta_store.get(media_id)
    if meta is None:
        raise MediaNotFoundError(f"no pending media {media_id!r} to bind")
    item = MediaItem(
        kind=meta.kind,
        url=f"{MEDIA_ROUTE_PREFIX}{media_id}",
        caption=None,
        filename=meta.filename if meta.kind is MediaKind.DOCUMENT else None,
    )
    return IngestedMedia(
        item=item, media_id=media_id, size=meta.size, sha256=meta.sha256, mime=meta.mime, pending=False
    )
