"""The media-by-reference store: decode-and-hold, read-back, substitution, TTL, and
the loud refusals of a bad mime or malformed base64."""

from __future__ import annotations

import base64

import pytest
from tai42_contract.interactions import (
    MEDIA_MAX_ITEMS,
    MEDIA_ROUTE_PREFIX,
    MEDIA_TOTAL_URI_CHARS,
    MediaItem,
    MediaKind,
)

from tai42_skeleton.interactions.media import read_media, store_data_image, substitute_media
from tai42_skeleton.interactions.store import InteractionStore

_PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()
_DATA_PNG = f"data:image/png;base64,{_PNG}"


async def test_store_and_read_round_trips_the_bytes(fake_redis):
    store = InteractionStore("t:")
    media_id = await store_data_image(store, fake_redis, _DATA_PNG, ttl_seconds=100)
    assert len(media_id) == 43  # urlsafe-base64 of 32 random bytes
    got = await read_media(store, fake_redis, media_id)
    assert got is not None
    mime, payload = got
    assert mime == "image/png"
    assert payload == bytes.fromhex("89504e470d0a1a0a")


async def test_read_missing_returns_none(fake_redis):
    store = InteractionStore("t:")
    assert await read_media(store, fake_redis, "a" * 43) is None


async def test_store_sets_the_ttl(fake_redis):
    store = InteractionStore("t:")
    media_id = await store_data_image(store, fake_redis, _DATA_PNG, ttl_seconds=30)
    assert await fake_redis.ttl(store.media_key(media_id)) == 30
    fake_redis.advance(31)
    assert await read_media(store, fake_redis, media_id) is None  # expired by TTL


@pytest.mark.parametrize(
    "bad",
    [
        "data:application/pdf;base64,AAAA",  # non-image mime
        "data:image/svg+xml;base64,AAAA",  # unlisted subtype
        "data:image/png,not-base64-marked",  # missing ;base64 marker
        "data:image/png;base64,@@@notbase64",  # not strict base64
        "data:image/png;base64,",  # empty payload
        "https://host/x.png",  # not a data: url at all
    ],
)
async def test_store_refuses_bad_input_loudly(fake_redis, bad):
    store = InteractionStore("t:")
    with pytest.raises(ValueError, match="media data url"):
        await store_data_image(store, fake_redis, bad, ttl_seconds=100)


async def test_substitute_swaps_data_image_for_relative_reference(fake_redis):
    store = InteractionStore("t:")
    items: list[MediaItem | dict] = [
        {"kind": "image", "url": _DATA_PNG, "caption": "shot"},
        {"kind": "image", "url": "https://cdn.example/x.png"},
        {"kind": "link", "url": "https://docs.example/p"},
    ]
    result = await substitute_media(store, fake_redis, items, ttl_seconds=100)
    # The data: image is swapped for a same-origin served reference; https/link unchanged.
    assert result[0].url.startswith(MEDIA_ROUTE_PREFIX)
    assert result[0].caption == "shot"
    assert result[1].url == "https://cdn.example/x.png"
    assert result[2].kind is MediaKind.LINK
    # The bytes are held under the new id and read back with the right mime.
    media_id = result[0].url[len(MEDIA_ROUTE_PREFIX) :]
    got = await read_media(store, fake_redis, media_id)
    assert got is not None
    assert got[0] == "image/png"


async def test_substitute_absolute_url_for_a_channel_send(fake_redis):
    store = InteractionStore("t:")
    items: list[MediaItem | dict] = [{"kind": "image", "url": _DATA_PNG}]
    result = await substitute_media(store, fake_redis, items, ttl_seconds=100, base_url="https://box.example/")
    assert result[0].url.startswith("https://box.example" + MEDIA_ROUTE_PREFIX)


async def test_substitute_absolutizes_relative_route_ref_all_kinds(fake_redis):
    # An ALREADY-STORED same-origin route reference (any media kind) is made absolute against
    # base_url for an off-origin channel fetch — not only a freshly-decoded data: image. caption
    # and filename ride the rewrite unchanged.
    store = InteractionStore("t:")
    doc_ref = MEDIA_ROUTE_PREFIX + "d" * 43
    img_ref = MEDIA_ROUTE_PREFIX + "i" * 43
    items: list[MediaItem | dict] = [
        {"kind": "document", "url": doc_ref, "filename": "receipt.pdf"},
        {"kind": "image", "url": img_ref, "caption": "shot"},
    ]
    result = await substitute_media(store, fake_redis, items, ttl_seconds=100, base_url="https://box.example/")
    assert result[0].url == "https://box.example" + doc_ref
    assert result[0].filename == "receipt.pdf"
    assert result[1].url == "https://box.example" + img_ref
    assert result[1].caption == "shot"


async def test_substitute_relative_route_ref_passes_through_without_base_url(fake_redis):
    # With no base_url (inbox-only) a relative route reference passes through unchanged — the
    # pre-existing same-origin behavior.
    store = InteractionStore("t:")
    doc_ref = MEDIA_ROUTE_PREFIX + "d" * 43
    items: list[MediaItem | dict] = [{"kind": "document", "url": doc_ref, "filename": "r.pdf"}]
    result = await substitute_media(store, fake_redis, items, ttl_seconds=100)
    assert result[0].url == doc_ref


async def test_substitute_already_absolute_served_url_is_idempotent(fake_redis):
    # An already-absolute served url does not start with MEDIA_ROUTE_PREFIX, so the rewrite
    # leaves it untouched — re-running the substitution never double-prefixes.
    store = InteractionStore("t:")
    absolute = "https://box.example" + MEDIA_ROUTE_PREFIX + "d" * 43
    items: list[MediaItem | dict] = [{"kind": "document", "url": absolute, "filename": "r.pdf"}]
    result = await substitute_media(store, fake_redis, items, ttl_seconds=100, base_url="https://box.example/")
    assert result[0].url == absolute


async def test_substitute_is_pure_over_the_input(fake_redis):
    store = InteractionStore("t:")
    original: list[MediaItem | dict] = [{"kind": "image", "url": _DATA_PNG}]
    result = await substitute_media(store, fake_redis, original, ttl_seconds=100)
    # A new list of MediaItem is returned; the input is not the same object.
    assert result is not original
    assert all(isinstance(item, MediaItem) for item in result)


def _big_data_png(url_chars: int) -> str:
    # A valid strict-base64 data:image/png URI whose total url length is ``url_chars``.
    header = "data:image/png;base64,"
    b64_len = url_chars - len(header)
    raw = b"\x00" * (b64_len // 4 * 3)
    return header + base64.b64encode(raw).decode()


async def _stored_media_keys(fake_redis) -> list[str]:
    return [k for k in fake_redis._hashes if k.startswith("t:media:")]


async def test_substitute_over_total_budget_raises_before_any_store(fake_redis):
    store = InteractionStore("t:")
    # 3x400 KiB data images exceed the summed-URI budget; the list-level cap must refuse
    # BEFORE the first image is decoded and held — no store write leaks past the guard.
    big = _big_data_png(400 * 1024)
    items: list[dict] = [{"kind": "image", "url": big} for _ in range(3)]
    assert sum(len(i["url"]) for i in items) > MEDIA_TOTAL_URI_CHARS
    with pytest.raises(ValueError, match="media total url length must be at most"):
        await substitute_media(store, fake_redis, items, ttl_seconds=100)
    assert await _stored_media_keys(fake_redis) == []  # nothing written to Redis


async def test_substitute_over_item_count_raises_before_any_store(fake_redis):
    store = InteractionStore("t:")
    # One past the item-count guard is refused before any image is stored.
    items: list[dict] = [{"kind": "image", "url": _DATA_PNG} for _ in range(MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(ValueError, match="media carries at most"):
        await substitute_media(store, fake_redis, items, ttl_seconds=100)
    assert await _stored_media_keys(fake_redis) == []  # refused before any store
