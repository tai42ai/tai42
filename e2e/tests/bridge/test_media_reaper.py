"""The inbound-media retention reaper deletes a record's blob past its horizon; a live record keeps serving.

An ingested media is served by its capability id until its retention horizon; past it, the media
reaper — the SOLE deleter of the blob + metadata — reclaims it, and the served url then 404s. A record
still within its horizon keeps serving through the same reaper passes.

The horizon is a durable per-media expiry-index score (member = media_id, score = epoch seconds).
Advancing ONE record past its horizon is a ZADD of a past score onto that index (the same durable
control the reaper reads), leaving every other record at its full horizon; the reaper interval is
pinned to 1s on this stack, so a pass lands within the test. Driven over the live media-bridge stack
(telegram ingest + storage-local + the redis conversations backend + the spawned reaper loop): two
photos to one route (two threads) ingest two served media; one is advanced past its horizon and reaped.

The channels mock over their in-process stubs, so any real selection breaks the stubs and steps aside.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.channel_stubs import TINY_PNG, MediaBlob
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    TELEGRAM_INBOUND_PATH,
    BridgeHarness,
    get_served_media,
    post_inbound,
    wait_probe_entries,
)

pytestmark = pytest.mark.skipif(
    any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp", "llm")),
    reason="the media-bridge stubs are the mock leg; real legs run on the creds host",
)


def _expire_media(bridge: BridgeHarness, media_id: str) -> None:
    """Advance ONE media's retention horizon into the past by rescoring its durable expiry-index
    member — the reaper reads this same index, so its next pass reaps this record and only this one."""
    import redis

    prefix = bridge.stack.config.env["CONVERSATIONS_PREFIX"]
    client = redis.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
    try:
        moved = client.zadd(f"{prefix}:media-meta:expiry", {media_id: 1.0})
        # zadd returns the count of NEW members; a rescore of an existing member returns 0.
        assert moved == 0, f"expected to rescore an existing expiry member, not add one (got {moved})"
    finally:
        client.close()


async def test_reaper_deletes_expired_media_and_keeps_live_media(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    file_a = uniq("reap-a")
    file_b = uniq("reap-b")
    media_bridge.telegram.media[file_a] = MediaBlob(body=TINY_PNG, content_type="image/png")
    media_bridge.telegram.media[file_b] = MediaBlob(body=TINY_PNG, content_type="image/png")

    exec_key = uniq("reap-exec")
    await media_bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    probe = uniq("reap-probe")
    # One route (a telegram identity is routable once); two photos from two chats bridge two threads.
    await media_bridge.create_tool_channel_route(
        route_name=uniq("reap-route").replace("_", "-"),
        tool="e2e_record",
        execution_key=exec_key,
        channel="telegram",
        our_identity=media_bridge.telegram_our_identity,
        start_expr=f'{{key: "{probe}", value: (.attachments[0].url // "")}}',
        reply_expr='"ok"',
    )
    for chat_id, file_id in (("980001", file_a), ("980002", file_b)):
        inbound = media_bridge.telegram_inbound_photo(chat_id=chat_id, file_id=file_id)
        resp = await post_inbound(media_bridge.stack, TELEGRAM_INBOUND_PATH, inbound, port=media_bridge.stack.port_b)
        assert resp.status_code in (200, 204), resp.text

    entries = await wait_probe_entries(media_bridge, probe, 2)
    urls = [entry["value"] for entry in entries]
    assert all(url.startswith("/api/interactions/media/") for url in urls), urls
    expiring_url, live_url = urls

    # Both serve their bytes before any reaping.
    for url in urls:
        served = await get_served_media(media_bridge.stack, url)
        assert served.status_code == 200, (url, served.text)
        assert served.content == TINY_PNG

    # Advance only the first record past its horizon; the reaper (1s interval) reclaims its blob.
    _expire_media(media_bridge, expiring_url.rsplit("/", 1)[1])

    async def _reaped() -> bool | None:
        resp = await get_served_media(media_bridge.stack, expiring_url)
        return True if resp.status_code == 404 else None

    await wait_for_async(_reaped, deadline=20.0, message="the reaper never deleted the expired media's blob")

    # The live record — still within its horizon — keeps serving through the same reaper passes.
    still_live = await get_served_media(media_bridge.stack, live_url)
    assert still_live.status_code == 200, still_live.text
    assert still_live.content == TINY_PNG
