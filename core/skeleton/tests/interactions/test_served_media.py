"""The one reader of stored media by id across the inbound and outbound stores, and its facet door."""

from __future__ import annotations

import base64
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fakeredis import aioredis
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    MediaKind,
    MediaNotFoundError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    StoredMedia,
)

from tai42_skeleton.app.instance import build_app
from tai42_skeleton.conversations import media_meta as meta_module
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.interactions import served_media
from tai42_skeleton.interactions.media import store_data_image
from tai42_skeleton.interactions.served_media import load_served_media
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore

_PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 24
_DATA_PNG = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"
_NOT_FOUND = "stored media not found"


class _CountingStorage:
    """A blob provider recording every ``load_bytes`` call."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.loads: list[str] = []

    async def load_bytes(self, path: str) -> bytes:
        self.loads.append(path)
        if path not in self.blobs:
            raise FileNotFoundError(path)
        return self.blobs[path]


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch, fake_redis, fake_client_ctx):
    # The outbound Redis store, configured; no conversations store, so no inbound record exists.
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    settings = InteractionsSettings(public_base_url="https://cb.example")
    monkeypatch.setattr(served_media, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(served_media, "interactions_settings", lambda: settings)
    monkeypatch.setattr(served_media, "interactions_store_configured", lambda: True)
    return SimpleNamespace(store=InteractionStore(settings.key_prefix), redis=fake_redis, monkeypatch=monkeypatch)


@pytest.fixture
async def inbound(outbound):
    # The inbound blob store too: a ``fakeredis[lua]`` metadata store and a counting blob provider.
    monkeypatch = outbound.monkeypatch
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    r = aioredis.FakeRedis(decode_responses=True)

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield r

    monkeypatch.setattr(meta_module, "client_ctx", fake_client_ctx)
    provider = _CountingStorage()
    monkeypatch.setattr(served_media, "_blob_provider", lambda: provider)
    try:
        yield SimpleNamespace(
            provider=provider,
            store=InboundMediaMetaStore(ConversationsSettings()),
            outbound=outbound,
            monkeypatch=monkeypatch,
        )
    finally:
        await r.aclose()


async def _seed_inbound(inbound, media_id: str, *, blob: bytes = _PNG, pending: bool = False, kind=MediaKind.IMAGE):
    await inbound.store.put(
        media_id,
        mime="image/png",
        size=len(blob),
        sha256="abc",
        filename="pic.png",
        kind=kind,
        storage_path=f"inbound-media/{media_id}",
        pending=pending,
        owner_channel_id="web",
        owner_participant_identity="alice",
        message_id=None if pending else "msg-1",
        expiry_at=time.time() + 1000,
    )
    inbound.provider.blobs[f"inbound-media/{media_id}"] = blob


async def test_an_outbound_hit_is_its_mime_and_bytes(outbound):
    media_id = await store_data_image(outbound.store, outbound.redis, _DATA_PNG, ttl_seconds=120)
    record = await load_served_media(media_id, max_bytes=None)
    assert (record.mime, record.data, record.kind, record.filename) == ("image/png", _PNG, MediaKind.IMAGE, None)
    assert 0 < record.expires_in <= 120


async def test_a_bound_inbound_hit_is_its_mime_bytes_kind_and_filename(inbound):
    media_id = "i" * 43
    await _seed_inbound(inbound, media_id, kind=MediaKind.DOCUMENT)
    record = await load_served_media(media_id, max_bytes=None)
    assert (record.mime, record.data, record.kind, record.filename) == (
        "image/png",
        _PNG,
        MediaKind.DOCUMENT,
        "pic.png",
    )
    assert 0 < record.expires_in <= 1000


async def test_a_malformed_id_is_not_found(inbound):
    with pytest.raises(MediaNotFoundError) as caught:
        await load_served_media("too-short", max_bytes=None)
    assert str(caught.value) == _NOT_FOUND
    assert inbound.provider.loads == []


async def test_an_unknown_id_is_not_found(inbound):
    with pytest.raises(MediaNotFoundError) as caught:
        await load_served_media("u" * 43, max_bytes=None)
    assert str(caught.value) == _NOT_FOUND


async def test_a_pending_inbound_item_is_not_found_and_its_bytes_are_never_loaded(inbound):
    media_id = "p" * 43
    await _seed_inbound(inbound, media_id, pending=True)
    with pytest.raises(MediaNotFoundError) as caught:
        await load_served_media(media_id, max_bytes=None)
    assert str(caught.value) == _NOT_FOUND
    assert inbound.provider.loads == []


async def test_an_inbound_record_whose_blob_is_absent_is_not_found(inbound):
    media_id = "b" * 43
    await _seed_inbound(inbound, media_id)
    inbound.provider.blobs.clear()
    with pytest.raises(MediaNotFoundError) as caught:
        await load_served_media(media_id, max_bytes=None)
    assert str(caught.value) == _NOT_FOUND


async def test_no_media_store_configured_is_not_found(outbound):
    outbound.monkeypatch.setattr(served_media, "interactions_store_configured", lambda: False)
    with pytest.raises(MediaNotFoundError) as caught:
        await load_served_media("n" * 43, max_bytes=None)
    assert str(caught.value) == _NOT_FOUND


async def test_an_inbound_record_with_no_blob_provider_is_unavailable(inbound):
    media_id = "v" * 43
    await _seed_inbound(inbound, media_id)
    inbound.monkeypatch.setattr(served_media, "_blob_provider", lambda: None)
    with pytest.raises(MediaStoreUnavailableError) as caught:
        await load_served_media(media_id, max_bytes=None)
    assert str(caught.value) == "media store unavailable"


async def test_the_not_found_text_never_names_the_id(inbound):
    media_id = "s" * 43
    await _seed_inbound(inbound, media_id, pending=True)
    with pytest.raises(MediaNotFoundError) as caught:
        await load_served_media(media_id, max_bytes=None)
    assert media_id not in str(caught.value)


async def test_the_too_large_text_never_names_the_id(inbound):
    media_id = "t" * 43
    await _seed_inbound(inbound, media_id)
    with pytest.raises(MediaTooLargeError) as caught:
        await load_served_media(media_id, max_bytes=len(_PNG) - 1)
    assert media_id not in str(caught.value)


async def test_an_inbound_item_over_max_bytes_is_refused_from_its_record_alone(inbound):
    media_id = "l" * 43
    await _seed_inbound(inbound, media_id)
    with pytest.raises(MediaTooLargeError) as caught:
        await load_served_media(media_id, max_bytes=len(_PNG) - 1)
    assert str(caught.value) == f"stored media is {len(_PNG)} bytes, over the {len(_PNG) - 1}-byte limit"
    assert inbound.provider.loads == []


async def test_an_outbound_item_over_max_bytes_is_refused(outbound):
    media_id = await store_data_image(outbound.store, outbound.redis, _DATA_PNG, ttl_seconds=120)
    with pytest.raises(MediaTooLargeError) as caught:
        await load_served_media(media_id, max_bytes=len(_PNG) - 1)
    assert str(caught.value) == f"stored media is {len(_PNG)} bytes, over the {len(_PNG) - 1}-byte limit"


async def test_an_item_of_exactly_max_bytes_is_read(inbound):
    media_id = "e" * 43
    await _seed_inbound(inbound, media_id)
    assert (await load_served_media(media_id, max_bytes=len(_PNG))).data == _PNG
    outbound_id = await store_data_image(inbound.outbound.store, inbound.outbound.redis, _DATA_PNG, ttl_seconds=60)
    assert (await load_served_media(outbound_id, max_bytes=len(_PNG))).data == _PNG


async def test_no_bound_never_refuses_on_size(inbound):
    media_id = "z" * 43
    big = _PNG + b"\x00" * 100_000
    await _seed_inbound(inbound, media_id, blob=big)
    assert (await load_served_media(media_id, max_bytes=None)).data == big


async def test_the_bound_app_reads_a_stored_picture_through_its_media_facet(outbound):
    tai42_app.bind(build_app())
    media_id = await store_data_image(outbound.store, outbound.redis, _DATA_PNG, ttl_seconds=60)
    stored = await tai42_app.media.read_media(media_id, max_bytes=1_000)
    assert stored == StoredMedia(mime="image/png", data=_PNG)
    with pytest.raises(MediaTooLargeError):
        await tai42_app.media.read_media(media_id, max_bytes=1)
    with pytest.raises(MediaNotFoundError):
        await tai42_app.media.read_media("q" * 43, max_bytes=1_000)
