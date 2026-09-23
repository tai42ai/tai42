"""Interactions served-media capability door and media substitution on the add path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from tai42_contract.interactions import MEDIA_ROUTE_PREFIX, InteractionResponse

from tai42_skeleton.interactions import ask
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.media import read_media
from tai42_skeleton.routers import interactions as router

from ..._helpers import await_add_event
from ._harness import _DATA_PNG, _PNG_BYTES, _store_media, make_request


async def test_media_route_serves_stored_bytes_with_headers(wired):
    media_id = "m" * 43
    await _store_media(wired, media_id, ttl=120)
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 200
    assert bytes(resp.body) == _PNG_BYTES
    assert resp.media_type == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # The client cache is bounded by the key's remaining lifetime (private, never shared).
    assert resp.headers["Cache-Control"] == "private, max-age=120"


async def test_media_route_bad_id_is_400(wired):
    resp = await router.media(make_request("GET", path_params={"media_id": "too-short"}))
    assert resp.status_code == 400


async def test_media_route_miss_is_404(wired):
    resp = await router.media(make_request("GET", path_params={"media_id": "a" * 43}))
    assert resp.status_code == 404


async def test_media_route_off_store_is_uniform_404(wired, monkeypatch):
    # An unconfigured store answers the SAME 404 as a miss — never a 501 that would
    # oracle the store's absence on this unauthenticated door.
    monkeypatch.setattr(router, "interactions_store_configured", lambda: False)
    resp = await router.media(make_request("GET", path_params={"media_id": "a" * 43}))
    assert resp.status_code == 404


async def test_ask_data_image_stored_by_reference(wired):
    # The ASK door substitutes a data:image BEFORE the request is built: the durable
    # record carries a same-origin served reference (never inline bytes), and the bytes
    # are readable by that id — the media round-trips through the store by reference.
    wired.monkeypatch.setattr(helper_module.secrets, "token_urlsafe", lambda n: "M" * 43)
    captured: dict = {}

    async def answer_when_asked() -> None:
        iid, gid = await await_add_event(wired.fake, wired.store)
        state = await wired.store.get_state(wired.fake, iid)
        assert state is not None
        captured["req"] = state.request
        await wired.store.record_answer(
            wired.fake,
            InteractionResponse(interaction_id=iid, answer="ok", answered_by="t", answered_at=datetime.now(UTC)),
            gid,
            reply_ttl=60,
        )

    answerer = asyncio.create_task(answer_when_asked())
    await ask("Pick", answer_format="text", media=[{"kind": "image", "url": _DATA_PNG}], timeout=5)
    await answerer

    req = captured["req"]
    assert req.media is not None
    assert req.media[0].url == MEDIA_ROUTE_PREFIX + "M" * 43  # a served reference, not the data: bytes
    got = await read_media(wired.store, wired.fake, "M" * 43)
    assert got is not None
    assert got[0] == "image/png"
