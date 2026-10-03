"""The served-media ingestion chokepoint ``ingest_media`` + the two-phase ``bind_media``.

Drives the seam against a fake blob provider and a real ``fakeredis[lua]`` metadata store, so the
caps, the sniff policy, the filename sanitiser, the record-first persist order and the two-phase
promote are exercised end to end.
"""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fakeredis import aioredis
from pydantic import ValidationError
from tai42_contract.conversations import build_inbound_media_params
from tai42_contract.interactions import MEDIA_ROUTE_PREFIX, MediaKind, MediaOrigin
from tai42_contract.interactions.models.media_errors import (
    MediaAlreadyBoundError,
    MediaNotFoundError,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.interactions import media_ingest_cap_settings

from tai42_skeleton.conversations import media_meta as meta_module
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.interactions import media_ingest
from tai42_skeleton.interactions.media_ingest import _sniff_isobmff, bind_media, ingest_media
from tai42_skeleton.settings.media_ingest import media_ingest_settings

# -- magic-byte fixtures (a valid header is enough for filetype) ---------------

_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 64
_JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * 64
_GIF = b"GIF89a" + b"\x00" * 64
_WEBP = b"RIFF" + (64).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 32
_OGG = b"OggS" + b"\x00" * 64
_MP3 = b"ID3" + b"\x00" * 64
_AAC = bytes([0xFF, 0xF1]) + b"\x00" * 64
_AMR = b"#!AMR\n" + b"\x00" * 64
_TIFF = bytes([0x49, 0x49, 0x2A, 0x00]) + b"\x00" * 64
_PDF = b"%PDF-1.4\n" + b"\x00" * 64
_HTML = b"<html><body>hi</body></html>"
_TXT = b"hello, world\nthis is plain text\n"
_CSV = b"a,b,c\n1,2,3\n"


def _isobmff(major: bytes, compatible: bytes = b"") -> bytes:
    body = b"ftyp" + major + b"\x00\x00\x00\x00" + compatible
    return (len(body) + 4).to_bytes(4, "big") + body


_MP4 = _isobmff(b"isom", b"isomiso2avc1mp41")
_QT = _isobmff(b"qt  ", b"qt  ")
_M4A = _isobmff(b"M4A ", b"M4A mp42isom")
_3GP = _isobmff(b"3gp5", b"3gp5")


def _ooxml(part: str, content_type: str) -> bytes:
    # A fixed entry timestamp keeps the sample bytes identical across processes, so a
    # parametrize id built from them is the same on every xdist worker.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        override = f'<Override PartName="/{part}" ContentType="{content_type}"/>'
        z.writestr(
            zipfile.ZipInfo("[Content_Types].xml", date_time=(1980, 1, 1, 0, 0, 0)),
            f'<?xml version="1.0"?><Types xmlns="x">{override}</Types>',
        )
        z.writestr(zipfile.ZipInfo(part, date_time=(1980, 1, 1, 0, 0, 0)), "<a/>")
    return buf.getvalue()


_OOXML_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_OOXML_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_OOXML_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOCX = _ooxml("word/document.xml", f"{_OOXML_DOCX}.main+xml")
_XLSX = _ooxml("xl/workbook.xml", f"{_OOXML_XLSX}.main+xml")
_PPTX = _ooxml("ppt/presentation.xml", f"{_OOXML_PPTX}.main+xml")


async def _gen(data: bytes, chunk: int = 4096) -> AsyncIterator[bytes]:
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


class _FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str | None]] = {}

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.blobs[path] = (data, content_type)

    async def load_bytes(self, path: str) -> bytes:
        if path not in self.blobs:
            raise FileNotFoundError(path)
        return self.blobs[path][0]

    async def delete(self, path: str) -> None:
        if path not in self.blobs:
            raise FileNotFoundError(path)
        del self.blobs[path]


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    media_ingest_settings.cache_clear()
    media_ingest_cap_settings.cache_clear()


@pytest.fixture(autouse=True)
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[aioredis.FakeRedis]:
    r = aioredis.FakeRedis(decode_responses=True)

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield r

    monkeypatch.setattr(meta_module, "client_ctx", fake_client_ctx)
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
def meta(client: aioredis.FakeRedis) -> InboundMediaMetaStore:
    return InboundMediaMetaStore(ConversationsSettings())


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _FakeStorage:
    store = _FakeStorage()
    monkeypatch.setattr(media_ingest, "_storage_provider", lambda: store)
    return store


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    captured: dict[str, list[dict]] = {"ingested": [], "rejected": []}

    async def _ingested(**kw):
        captured["ingested"].append(kw)

    async def _rejected(**kw):
        captured["rejected"].append(kw)

    monkeypatch.setattr(media_ingest, "emit_inbound_media_ingested", _ingested)
    monkeypatch.setattr(media_ingest, "emit_inbound_media_rejected", _rejected)
    return captured


def _origin(message_id: str | None = "msg-1", channel_id: str = "whatsapp", identity: str = "wa-1") -> MediaOrigin:
    return MediaOrigin(channel_id=channel_id, participant_identity=identity, message_id=message_id)


# -- ingest happy paths -------------------------------------------------------


async def test_ingests_image_returns_served_item(provider, meta, client):
    result = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=len(_PNG),
        origin=_origin(),
    )
    assert result.item.kind is MediaKind.IMAGE
    assert result.item.url == f"{MEDIA_ROUTE_PREFIX}{result.media_id}"
    assert result.mime == "image/png"
    assert result.size == len(_PNG)
    import hashlib

    assert result.sha256 == hashlib.sha256(_PNG).hexdigest()
    assert provider.blobs[f"inbound-media/{result.media_id}"][0] == _PNG
    # The meta hash carries NO Redis TTL; the durable index member is scored at the record horizon.
    assert await client.pttl(meta.meta_key(result.media_id)) == -1
    score = await meta.score(result.media_id)
    assert score is not None
    assert score > time.time() + 29 * 86400


async def test_document_sets_filename_on_item(provider, meta):
    doc = await ingest_media(
        source=_gen(_PDF),
        kind_hint="document",
        declared_mime="application/pdf",
        filename="r.pdf",
        declared_size=None,
        origin=_origin(),
    )
    assert doc.item.kind is MediaKind.DOCUMENT
    assert doc.item.filename == "r.pdf"
    # An image with a filename carries None on the item, but the meta record keeps the sanitised name.
    img = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename="photo.png",
        declared_size=None,
        origin=_origin(),
    )
    assert img.item.filename is None
    stored = await meta.get(img.media_id)
    assert stored is not None
    assert stored.filename == "photo.png"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a" * 396 + ".pdf", lambda name: len(name) <= 255 and name.endswith(".pdf")),
        ("a" * 400, lambda name: len(name) == 255 and "." not in name),
        ("a" * 400 + "." + "b" * 20, lambda name: len(name) == 255 and not name.endswith("b" * 20)),
    ],
    ids=["ext-kept", "no-dot-whole", "over-long-ext-not-preserved"],
)
async def test_over_length_filename_truncation(provider, raw, expected):
    doc = await ingest_media(
        source=_gen(_PDF),
        kind_hint="document",
        declared_mime="application/pdf",
        filename=raw,
        declared_size=None,
        origin=_origin(),
    )
    assert expected(doc.item.filename)


async def test_control_char_and_newline_filename_sanitised(provider):
    doc = await ingest_media(
        source=_gen(_PDF),
        kind_hint="document",
        declared_mime="application/pdf",
        filename="\nre\rp\x00ort.pdf ",
        declared_size=None,
        origin=_origin(),
    )
    assert doc.item.filename == "report.pdf"


async def test_bidi_control_stripped(provider):
    doc = await ingest_media(
        source=_gen(_PDF),
        kind_hint="document",
        declared_mime="application/pdf",
        filename="re‮port.pdf",
        declared_size=None,
        origin=_origin(),
    )
    assert doc.item.filename is not None
    assert "‮" not in doc.item.filename
    assert doc.item.filename == "report.pdf"


async def test_blank_or_absent_filename_gets_generic_kind_name(provider):
    for raw in (None, "   \n"):
        doc = await ingest_media(
            source=_gen(_PDF),
            kind_hint="document",
            declared_mime="application/pdf",
            filename=raw,
            declared_size=None,
            origin=_origin(),
        )
        assert doc.item.filename == "document.pdf"
    txt = await ingest_media(
        source=_gen(_TXT),
        kind_hint="document",
        declared_mime="text/plain",
        filename=None,
        declared_size=None,
        origin=_origin(),
    )
    assert txt.item.filename == "document.txt"


async def test_parity_media_filename_equals_sanitised(provider):
    doc = await ingest_media(
        source=_gen(_PDF),
        kind_hint="document",
        declared_mime="application/pdf",
        filename="re\rport.pdf",
        declared_size=None,
        origin=_origin(),
    )
    params = build_inbound_media_params(kind="document", media_id=doc.media_id, filename=doc.item.filename)
    assert params["media_filename"] == doc.item.filename == "report.pdf"


async def test_programmer_error_from_bad_sanitiser_propagates(provider, monkeypatch):
    monkeypatch.setattr(media_ingest, "_sanitize_filename", lambda *a, **k: "x" * 256)
    with pytest.raises(ValidationError):
        await ingest_media(
            source=_gen(_PDF),
            kind_hint="document",
            declared_mime="application/pdf",
            filename="ok.pdf",
            declared_size=None,
            origin=_origin(),
        )


# -- caps ---------------------------------------------------------------------


async def test_declared_size_over_cap_raises_before_read(provider, monkeypatch):
    consumed = False

    async def _spy() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield _PNG

    with pytest.raises(MediaTooLargeError):
        await ingest_media(
            source=_spy(),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=9 * 1024 * 1024,
            origin=_origin(),
        )
    assert consumed is False
    assert provider.blobs == {}


async def test_streamed_body_over_cap_raises_not_truncates(provider, monkeypatch):
    monkeypatch.setenv("MEDIA_INGEST_MAX_IMAGE_BYTES", "10000")
    media_ingest_cap_settings.cache_clear()
    body = _PNG + b"\x00" * 20000
    with pytest.raises(MediaTooLargeError):
        await ingest_media(
            source=_gen(body),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )
    assert provider.blobs == {}


async def test_sniffed_kind_cap_applies_after_early_sniff(provider, monkeypatch):
    # A web door (kind_hint None) sees the largest cap up front; after the early sniff derives IMAGE
    # the tighter image cap is applied to the remainder.
    monkeypatch.setenv("MEDIA_INGEST_MAX_IMAGE_BYTES", "10000")
    media_ingest_cap_settings.cache_clear()
    body = _PNG + b"\x00" * 20000
    with pytest.raises(MediaTooLargeError):
        await ingest_media(
            source=_gen(body),
            kind_hint=None,
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )
    assert provider.blobs == {}


# -- sniff policy -------------------------------------------------------------


async def test_declared_image_none_sniff_rejected(provider, events):
    with pytest.raises(MediaTypeNotAllowedError):
        await ingest_media(
            source=_gen(_HTML),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )
    assert events["rejected"][0]["reason"].value == "unsupported_type"


async def test_declared_image_sniffs_different_image_accepted_as_sniffed(provider, meta):
    # Declared image/png, bytes are a real JPEG: the top-level types agree, the SNIFFED type is
    # authoritative — accepted and stored/served as image/jpeg.
    result = await ingest_media(
        source=_gen(_JPEG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(),
    )
    assert result.item.kind is MediaKind.IMAGE
    assert result.mime == "image/jpeg"
    stored = await meta.get(result.media_id)
    assert stored is not None
    assert stored.mime == "image/jpeg"
    assert provider.blobs[f"inbound-media/{result.media_id}"][1] == "image/jpeg"


async def test_declared_image_sniffs_cross_top_level_rejected(provider):
    # Declared image/png, bytes are a PDF: the top-level types disagree -> rejected.
    with pytest.raises(MediaTypeNotAllowedError):
        await ingest_media(
            source=_gen(_PDF),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )


async def test_declared_image_sniffs_not_allowlisted_rejected(provider):
    # Declared image/png, bytes sniff image/tiff: same top-level, but the sniffed type is not on
    # the image allowlist -> rejected.
    with pytest.raises(MediaTypeNotAllowedError):
        await ingest_media(
            source=_gen(_TIFF),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )


@pytest.mark.parametrize(("body", "declared"), [(_HTML, "text/html"), (b"<svg/>" + b"\x00" * 32, "image/svg+xml")])
async def test_html_or_svg_declared_as_document_rejected(provider, body, declared):
    with pytest.raises(MediaTypeNotAllowedError):
        await ingest_media(
            source=_gen(body),
            kind_hint="document",
            declared_mime=declared,
            filename=None,
            declared_size=None,
            origin=_origin(),
        )


async def test_textlike_document_none_sniff_accepted(provider):
    for body, declared in ((_TXT, "text/plain"), (_CSV, "text/csv")):
        doc = await ingest_media(
            source=_gen(body),
            kind_hint="document",
            declared_mime=declared,
            filename="f",
            declared_size=None,
            origin=_origin(),
        )
        assert doc.item.kind is MediaKind.DOCUMENT
        assert doc.mime == declared
    with pytest.raises(MediaTypeNotAllowedError):
        await ingest_media(
            source=_gen(_TXT),
            kind_hint="document",
            declared_mime="application/x-unknown",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )


async def test_pdf_sniff_agrees_accepted(provider):
    doc = await ingest_media(
        source=_gen(_PDF),
        kind_hint="document",
        declared_mime="application/pdf",
        filename="r.pdf",
        declared_size=None,
        origin=_origin(),
    )
    assert doc.item.kind is MediaKind.DOCUMENT
    assert doc.mime == "application/pdf"


_BINARY_SAMPLES = [
    (_PNG, "image/png", MediaKind.IMAGE),
    (_JPEG, "image/jpeg", MediaKind.IMAGE),
    (_GIF, "image/gif", MediaKind.IMAGE),
    (_WEBP, "image/webp", MediaKind.IMAGE),
    (_OGG, "audio/ogg", MediaKind.AUDIO),
    (_MP3, "audio/mpeg", MediaKind.AUDIO),
    (_M4A, "audio/mp4", MediaKind.AUDIO),
    (_AAC, "audio/aac", MediaKind.AUDIO),
    (_AMR, "audio/amr", MediaKind.AUDIO),
    (_MP4, "video/mp4", MediaKind.VIDEO),
    (_QT, "video/quicktime", MediaKind.VIDEO),
    (_DOCX, _OOXML_DOCX, MediaKind.DOCUMENT),
    (_XLSX, _OOXML_XLSX, MediaKind.DOCUMENT),
    (_PPTX, _OOXML_PPTX, MediaKind.DOCUMENT),
]


@pytest.mark.parametrize(
    ("body", "declared", "kind"), _BINARY_SAMPLES, ids=[declared for _, declared, _ in _BINARY_SAMPLES]
)
async def test_every_allowlisted_binary_type_accepts_synthetic_sample(provider, body, declared, kind):
    result = await ingest_media(
        source=_gen(body),
        kind_hint=None,
        declared_mime=declared,
        filename="f",
        declared_size=None,
        origin=_origin(),
    )
    assert result.item.kind is kind
    assert result.mime == declared


async def test_plain_3gp_accepts_via_isobmff_matcher(provider):
    result = await ingest_media(
        source=_gen(_3GP),
        kind_hint="video",
        declared_mime="video/3gpp",
        filename=None,
        declared_size=None,
        origin=_origin(),
    )
    assert result.item.kind is MediaKind.VIDEO
    assert result.mime == "video/3gpp"


# -- the ISOBMFF brand matcher (unit) -----------------------------------------


def test_isobmff_brand_matcher_maps_brands():
    assert _sniff_isobmff(_isobmff(b"3gp5")) == "video/3gpp"
    assert _sniff_isobmff(_isobmff(b"3g2a")) == "video/3gpp2"
    assert _sniff_isobmff(_isobmff(b"mp42")) == "video/mp4"
    assert _sniff_isobmff(_isobmff(b"isom")) == "video/mp4"
    assert _sniff_isobmff(_isobmff(b"avc1")) == "video/mp4"
    assert _sniff_isobmff(_isobmff(b"M4V ")) == "video/mp4"
    assert _sniff_isobmff(_isobmff(b"M4A ")) == "audio/mp4"
    assert _sniff_isobmff(_isobmff(b"qt  ")) == "video/quicktime"
    assert _sniff_isobmff(b"not an ftyp box at all") is None
    assert _sniff_isobmff(_isobmff(b"zzzz")) is None


def test_isobmff_matcher_rejects_box_size_beyond_buffer():
    # A 4 GB box-size field over a short buffer -> None, no unbounded compatible-brand loop.
    crafted = b"\xff\xff\xff\xff" + b"ftyp" + b"3gp5" + b"\x00" * 4
    started = time.monotonic()
    assert _sniff_isobmff(crafted) is None
    assert time.monotonic() - started < 0.5


def test_isobmff_matcher_rejects_box_size_beyond_window():
    body = b"ftyp" + b"3gp5" + b"\x00\x00\x00\x00" + b"3gp5"
    crafted = (9000).to_bytes(4, "big") + body  # declared box size exceeds the 8192 sniff window
    assert _sniff_isobmff(crafted) is None


# -- store-unavailable / source faults ----------------------------------------


async def test_no_provider_raises_store_unavailable(events, monkeypatch):
    monkeypatch.setattr(media_ingest, "_storage_provider", lambda: None)
    with pytest.raises(MediaStoreUnavailableError):
        await ingest_media(
            source=_gen(_PNG),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )
    assert events["rejected"][0]["reason"].value == "could_not_receive"


async def test_source_fault_raises_source_read_error(provider):
    async def _torn() -> AsyncIterator[bytes]:
        yield _PNG[:16]
        raise ConnectionResetError("vendor stream torn")

    with pytest.raises(MediaSourceReadError):
        await ingest_media(
            source=_torn(),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )


async def test_upload_fault_after_record_write_raises_typed_and_leaves_record_for_reaper(meta, monkeypatch):
    class _FailingUpload(_FakeStorage):
        async def upload_bytes(self, path, data, content_type=None):
            raise RuntimeError("store boundary fault")

    failing = _FailingUpload()
    monkeypatch.setattr(media_ingest, "_storage_provider", lambda: failing)
    with pytest.raises(MediaStoreUnavailableError):
        await ingest_media(
            source=_gen(_PNG),
            kind_hint="image",
            declared_mime="image/png",
            filename=None,
            declared_size=None,
            origin=_origin(),
        )
    # The metadata record + expiry member survive for the reaper; no blob exists.
    due = await meta.due_expiry_ids(time.time() + 10**9)
    assert len(due) == 1
    stored = await meta.get(due[0])
    assert stored is not None
    assert due[0] not in failing.blobs


# -- two-phase pending / bind -------------------------------------------------


async def test_ingest_with_message_id_is_bound_at_ingest(provider, meta, client):
    result = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id="m-bound"),
    )
    assert result.pending is False
    stored = await meta.get(result.media_id)
    assert stored is not None
    assert stored.pending is False
    assert await client.pttl(meta.meta_key(result.media_id)) == -1
    score = await meta.score(result.media_id)
    assert score is not None
    assert score > time.time() + 29 * 86400


async def test_ingest_without_message_id_is_pending(provider, meta):
    result = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id=None, channel_id="web", identity="sess-1"),
    )
    assert result.pending is True
    stored = await meta.get(result.media_id)
    assert stored is not None
    assert stored.pending is True
    assert stored.owner_channel_id == "web"
    assert stored.owner_participant_identity == "sess-1"
    score = await meta.score(result.media_id)
    assert score is not None
    assert score < time.time() + 1000


async def test_bind_pending_promotes_to_record_horizon(provider, meta):
    pending = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id=None, channel_id="web", identity="sess-1"),
    )
    bound = await bind_media(pending.media_id, origin=_origin(message_id="m-1", channel_id="web", identity="sess-1"))
    assert bound.pending is False
    assert bound.media_id == pending.media_id
    stored = await meta.get(pending.media_id)
    assert stored is not None
    assert stored.message_id == "m-1"
    score = await meta.score(pending.media_id)
    assert score is not None
    assert score > time.time() + 29 * 86400


async def test_bind_expired_pending_raises_not_found(provider, meta):
    pending = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id=None, channel_id="web", identity="sess-1"),
    )
    # Simulate the reaper having deleted the past-horizon pending record.
    await meta.delete(pending.media_id)
    with pytest.raises(MediaNotFoundError):
        await bind_media(pending.media_id, origin=_origin(message_id="m-1", channel_id="web", identity="sess-1"))


async def test_bind_wrong_owner_raises_not_found(provider):
    pending = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id=None, channel_id="web", identity="sess-1"),
    )
    with pytest.raises(MediaNotFoundError):
        await bind_media(pending.media_id, origin=_origin(message_id="m-1", channel_id="web", identity="other"))


async def test_rebind_same_message_is_idempotent(provider):
    pending = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id=None, channel_id="web", identity="sess-1"),
    )
    origin = _origin(message_id="m-1", channel_id="web", identity="sess-1")
    first = await bind_media(pending.media_id, origin=origin)
    again = await bind_media(pending.media_id, origin=origin)
    assert first.media_id == again.media_id
    assert again.pending is False


async def test_rebind_different_message_raises_already_bound(provider):
    pending = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(message_id=None, channel_id="web", identity="sess-1"),
    )
    await bind_media(pending.media_id, origin=_origin(message_id="m-1", channel_id="web", identity="sess-1"))
    with pytest.raises(MediaAlreadyBoundError):
        await bind_media(pending.media_id, origin=_origin(message_id="m-2", channel_id="web", identity="sess-1"))


async def test_bind_requires_message_id(provider):
    with pytest.raises(ValueError, match=r"requires origin\.message_id"):
        await bind_media("whatever", origin=_origin(message_id=None, channel_id="web", identity="sess-1"))


# -- ingest / reaper path agreement -------------------------------------------


async def test_ingest_writes_the_exact_path_the_reaper_deletes(provider, meta):
    """The blob path ``ingest_media`` writes is the one ``_reap_one_media`` deletes — one derivation.

    The provider's ``delete`` raises ``FileNotFoundError`` on a miss, so a reaper that derived a
    different path would leave the blob; that the blob and its record are both gone pins agreement.
    """
    from tai42_skeleton.conversations.media_reaper import _reap_one_media

    result = await ingest_media(
        source=_gen(_PNG),
        kind_hint="image",
        declared_mime="image/png",
        filename=None,
        declared_size=None,
        origin=_origin(),
    )
    assert list(provider.blobs) == [meta_module.inbound_media_blob_path(result.media_id)]

    await _reap_one_media(provider, meta, result.media_id)

    assert provider.blobs == {}
    assert await meta.get(result.media_id) is None
