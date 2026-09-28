"""Interactions served-media capability door and media substitution on the add path."""

from __future__ import annotations

import asyncio
import importlib
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fakeredis import aioredis
from tai42_contract.interactions import MEDIA_ROUTE_PREFIX, InteractionResponse, MediaKind

from tai42_skeleton.conversations import media_meta as meta_module
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.interactions import ask
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.media import read_media
from tai42_skeleton.routers import interactions as router

from ..._helpers import await_add_event
from ._harness import _DATA_PNG, _PNG_BYTES, _store_media, make_request

# The package re-exports the ``media`` route callable, shadowing the submodule attribute;
# resolve the actual submodule so a ``_blob_provider`` patch lands in the route's globals.
media_mod = importlib.import_module("tai42_skeleton.routers.interactions.media")


async def test_media_route_serves_stored_bytes_with_headers(wired):
    media_id = "m" * 43
    await _store_media(wired, media_id, ttl=120)
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 200
    assert bytes(resp.body) == _PNG_BYTES
    assert resp.media_type == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # Served bytes are sandboxed by CSP (defense in depth), the outbound store no exception.
    assert resp.headers["Content-Security-Policy"] == "sandbox"
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


class _FakeStorage:
    """A blob provider standing in for the served route's ``load_bytes``."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def load_bytes(self, path: str) -> bytes:
        if path not in self.blobs:
            raise FileNotFoundError(path)
        return self.blobs[path]


@pytest.fixture
async def inbound(wired):
    # The inbound blob store: a real ``fakeredis[lua]`` metadata store (so ``put``'s atomic
    # script runs) plus a fake blob provider. ``wired`` also wires the outbound store, so a
    # no-record lookup falls through to a real 404 rather than a live connection.
    monkeypatch = wired.monkeypatch
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    r = aioredis.FakeRedis(decode_responses=True)

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield r

    monkeypatch.setattr(meta_module, "client_ctx", fake_client_ctx)
    provider = _FakeStorage()
    monkeypatch.setattr(media_mod, "_blob_provider", lambda: provider)
    store = InboundMediaMetaStore(ConversationsSettings())
    try:
        yield SimpleNamespace(redis=r, provider=provider, store=store, monkeypatch=monkeypatch)
    finally:
        await r.aclose()


async def _seed_inbound(inbound, media_id, *, kind, mime, filename, pending=False, blob=_PNG_BYTES):
    now = time.time()
    await inbound.store.put(
        media_id,
        mime=mime,
        size=len(blob),
        sha256="abc",
        filename=filename,
        kind=kind,
        storage_path=f"inbound-media/{media_id}",
        pending=pending,
        owner_channel_id="web",
        owner_participant_identity="sess-1",
        message_id=None if pending else "msg-1",
        expiry_at=now + 1000,
    )
    inbound.provider.blobs[f"inbound-media/{media_id}"] = blob


async def test_serves_inbound_blob_by_capability_id(inbound):
    media_id = "i" * 43
    await _seed_inbound(inbound, media_id, kind=MediaKind.IMAGE, mime="image/png", filename="pic.png")
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 200
    assert bytes(resp.body) == _PNG_BYTES
    assert resp.media_type == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    # An image renders inline — no attachment disposition.
    assert "Content-Disposition" not in resp.headers


async def test_inbound_document_forces_attachment_disposition(inbound):
    media_id = "d" * 43
    await _seed_inbound(
        inbound, media_id, kind=MediaKind.DOCUMENT, mime="application/pdf", filename="café☕.pdf", blob=b"%PDF-1.4"
    )
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 200
    assert (
        resp.headers["Content-Disposition"]
        == "attachment; filename=\"caf__.pdf\"; filename*=UTF-8''caf%C3%A9%E2%98%95.pdf"
    )
    assert resp.headers["Content-Security-Policy"] == "sandbox"


async def test_served_route_head_returns_headers_without_body(inbound):
    media_id = "h" * 43
    await _seed_inbound(
        inbound, media_id, kind=MediaKind.DOCUMENT, mime="application/pdf", filename="doc.pdf", blob=b"%PDF"
    )
    resp = await router.media(make_request("HEAD", path_params={"media_id": media_id}))
    assert resp.status_code == 200
    assert bytes(resp.body) == b""
    assert resp.media_type == "application/pdf"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    assert resp.headers["Content-Disposition"].startswith('attachment; filename="doc.pdf"')
    # Same malformed (400) / miss (404) behaviour as the GET.
    assert (await router.media(make_request("HEAD", path_params={"media_id": "too-short"}))).status_code == 400
    assert (await router.media(make_request("HEAD", path_params={"media_id": "z" * 43}))).status_code == 404


async def test_inbound_pending_item_is_404(inbound):
    # A capability id must not leak bytes before the owning message binds the pending item.
    media_id = "p" * 43
    await _seed_inbound(inbound, media_id, kind=MediaKind.IMAGE, mime="image/png", filename="pic.png", pending=True)
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 404


async def test_inbound_missing_blob_is_404(inbound):
    # The record is present but the blob never landed (or was reaped) — a uniform 404.
    media_id = "b" * 43
    await _seed_inbound(inbound, media_id, kind=MediaKind.IMAGE, mime="image/png", filename="pic.png")
    inbound.provider.blobs.clear()
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 404


async def test_inbound_record_without_provider_is_503(inbound):
    # A valid record but the blob provider is gone — an honest store-unavailable, not a 404
    # that would mask the outage (the id is valid, so there is no oracle to protect).
    media_id = "u" * 43
    await _seed_inbound(inbound, media_id, kind=MediaKind.IMAGE, mime="image/png", filename="pic.png")
    inbound.monkeypatch.setattr(media_mod, "_blob_provider", lambda: None)
    resp = await router.media(make_request("GET", path_params={"media_id": media_id}))
    assert resp.status_code == 503


async def test_no_provider_inbound_lookup_is_404_not_oracle(inbound):
    # No record + no provider → the SAME uniform 404 as any miss (the provider is never
    # consulted for an unknown id, so its absence cannot be oracled).
    inbound.monkeypatch.setattr(media_mod, "_blob_provider", lambda: None)
    resp = await router.media(make_request("GET", path_params={"media_id": "n" * 43}))
    assert resp.status_code == 404


async def test_inbound_unknown_id_falls_through_to_uniform_404(inbound):
    # Conversations store configured, no inbound record: falls through to the outbound
    # store, which also misses → 404; a malformed id is still a 400.
    assert (await router.media(make_request("GET", path_params={"media_id": "m" * 43}))).status_code == 404
    assert (await router.media(make_request("GET", path_params={"media_id": "nope"}))).status_code == 400


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
