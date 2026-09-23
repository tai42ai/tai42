"""Interactions paged pending-list door, the display-only media add-frame, and the
parked-interactions audit door."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.interactions import InteractionResponse, MediaItem

from tai42_skeleton.interactions import InteractionStore, ask
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.operations.interactions import _add_data
from tai42_skeleton.routers import interactions as router

from ..._helpers import await_add_event
from ._harness import (
    _EXPECTED_MEDIA_FRAME,
    _MEDIA_ITEMS,
    _async_park,
    _identity,
    _json,
    _media_request,
    _plain_request_dated,
    _seed,
    _seed_addressed,
    _sensitive_request,
    _store_empty,
    make_request,
)


async def test_list_add_carries_external_url(wired):
    await _seed(wired)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert page["items"], page
    assert page["items"][0]["format_payload"]["url"] == "https://ext.example/resource"


async def test_list_prunes_phantom_group(wired):
    # A group in the pending index whose stream expired must be pruned, not counted —
    # the list door prunes it — the reconciliation the pending read performs.
    wired.fake._zadd(wired.store.pending_key, {"gone": 1.0})
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert "gone" not in wired.fake._zsets.get(wired.store.pending_key, {})
    assert page["total"] == 0


async def test_list_reconciles_abandoned_past_deadline(wired):
    # A SIGKILLed waiter leaves a pending state past its deadline; the list must
    # NOT surface it and must reconcile the count/pending index (it self-heals). An
    # already-answered sibling is simply skipped.
    now = datetime.now(UTC)
    dead = _plain_request_dated(wired.store, "dead", "g", now - timedelta(seconds=120), now - timedelta(seconds=60))
    live = _plain_request_dated(wired.store, "live", "g", now, now + timedelta(seconds=60))
    done = _plain_request_dated(wired.store, "done", "g", now, now + timedelta(seconds=60))
    await wired.store.add(wired.fake, dead, idle_ttl=86400)
    await wired.store.add(wired.fake, live, idle_ttl=86400)
    await wired.store.add(wired.fake, done, idle_ttl=86400)
    done_resp = InteractionResponse(interaction_id="done", answer="x", answered_by="tester", answered_at=now)
    await wired.store.record_answer(wired.fake, done_resp, "g", reply_ttl=60)

    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    add_ids = [item["interaction_id"] for item in page["items"]]
    assert add_ids == ["live"]  # dead abandoned, done answered — only live surfaces

    # Reconciled: a removed event for the dead one, count decremented to the live
    # sibling (dead + done both gone from the count of 3), group still pending.
    events = await wired.fake.xrange(wired.store.events_key)
    removed_ids = [f["interaction_id"] for _id, f in events if f.get("type") == "interaction.removed"]
    assert "dead" in removed_ids
    assert wired.fake._strings[wired.store.count_key("g")] == "1"
    assert "g" in wired.fake._zsets[wired.store.pending_key]
    assert await wired.store.get_state(wired.fake, "dead") is None
    # The answered sibling is skipped, NOT pruned — it stays answered, no removed event.
    assert "done" not in removed_ids
    done_state = await wired.store.get_state(wired.fake, "done")
    assert done_state is not None
    assert done_state.status == "answered"


async def test_list_add_carries_sensitive_flag(wired):
    # The sensitive flag must reach the client on the list add frame so the UI
    # can label the answered state — not just live in a render fixture.
    await wired.store.add(wired.fake, _sensitive_request(wired.store), idle_ttl=86400)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert page["items"], page
    assert page["items"][0]["sensitive"] is True


async def test_list_non_sensitive_add_carries_sensitive_false(wired):
    # Regression: an ordinary question serializes sensitive: false, never omitted.
    await _seed(wired)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert page["items"], page
    assert page["items"][0]["sensitive"] is False


async def test_list_add_carries_media(wired):
    # The media list rides the list add frame as plain JSON dicts (exclude_none).
    await wired.store.add(wired.fake, _media_request(wired.store, media=_MEDIA_ITEMS), idle_ttl=86400)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert page["items"], page
    assert page["items"][0]["media"] == _EXPECTED_MEDIA_FRAME


async def test_list_pages_bounds_cap_and_next_page(wired):
    from tai42_skeleton.operations import BadRequestError

    for i in range(3):
        await _seed_addressed(wired, f"p{i}", f"g{i}", None)
    with _identity(user_id="op1", owner=None):
        page1 = await ops.list_interactions(page=1, page_size=2)
        assert len(page1["items"]) == 2
        assert page1["total"] == 3
        assert page1["next_page"] == 2  # off the indexed total, not the returned count
        assert page1["truncated"] is False
        page2 = await ops.list_interactions(page=2, page_size=2)
        assert len(page2["items"]) == 1
        assert page2["next_page"] is None  # last page
        # A page size above the cap is CAPPED to 200 (valid data), never refused.
        capped = await ops.list_interactions(page=1, page_size=5000)
        assert capped["page_size"] == 200
        # A malformed window is a loud 400.
        with pytest.raises(BadRequestError):
            await ops.list_interactions(page=0, page_size=1)
        with pytest.raises(BadRequestError):
            await ops.list_interactions(page=1, page_size=0)


async def test_list_off_store_answers_empty_page(wired, monkeypatch):
    from tai42_skeleton.operations import BadRequestError

    monkeypatch.setattr(ops, "interactions_store_configured", lambda: False)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions(page=1, page_size=5000)
        assert page == {"items": [], "total": 0, "page": 1, "page_size": 200, "next_page": None, "truncated": False}
        # A malformed window is a 400 even with the store OFF — no configured-vs-off oracle.
        with pytest.raises(BadRequestError):
            await ops.list_interactions(page=0, page_size=1)


async def test_list_add_without_media_omits_key(wired):
    # A question without media carries NO ``media`` key (absent, not null).
    await _seed(wired)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert page["items"], page
    assert "media" not in page["items"][0]


def test_add_data_media_is_conditional(wired):
    # The add frame carries ``media`` when the request has it and omits the key otherwise.
    with_media = _media_request(wired.store, media=_MEDIA_ITEMS)
    assert _add_data(with_media)["media"] == _EXPECTED_MEDIA_FRAME
    without_media = _media_request(wired.store)
    assert "media" not in _add_data(without_media)


async def test_ask_invalid_media_raises_before_persist(wired):
    # An invalid media item fails at the InteractionRequest build, before any state
    # is written: the request never persists and the open index stays empty.
    with pytest.raises(ValidationError):
        await ask("q", media=[{"kind": "image", "url": "javascript:alert(1)"}], timeout=5)
    assert await wired.store.count_open(wired.fake) == 0
    assert _store_empty(wired.fake)


async def test_ask_media_persists_and_frame_carries_it(wired):
    # End to end through the real helper with DICT-form media (an agent emits dicts):
    # the ask persists, answers normally (media never touches the answer), and the
    # stored request's add frame carries the coerced media — the display-only round trip.
    captured: dict = {}

    async def answer_when_asked() -> None:
        iid, gid = await await_add_event(wired.fake, wired.store)
        state = await wired.store.get_state(wired.fake, iid)
        assert state is not None
        captured["frame"] = _add_data(state.request)
        response = InteractionResponse(
            interaction_id=iid, answer="chosen", answered_by="tester", answered_at=datetime.now(UTC)
        )
        await wired.store.record_answer(wired.fake, response, gid, reply_ttl=60)

    media: list[MediaItem | dict[str, Any]] = [
        {"kind": "image", "url": "https://cdn.example/p.png", "caption": "A product"},
        {"kind": "link", "url": "https://docs.example/p"},
    ]
    answerer = asyncio.create_task(answer_when_asked())
    result = await ask("Pick a product", answer_format="text", media=media, timeout=5)
    await answerer

    assert result == "chosen"  # answer unchanged by the presence of media
    assert captured["frame"]["media"] == _EXPECTED_MEDIA_FRAME


async def test_list_media_frame_filtered_by_audience(wired):
    # Media rides the list item identically under the audience filter: a restricted caller
    # sees its OWN addressed media question (media intact) and NOT another identity's.
    mine = _media_request(wired.store, iid="mine", gid="gm", media=_MEDIA_ITEMS, audience="keyA")
    other = _media_request(wired.store, iid="other", gid="go", media=_MEDIA_ITEMS, audience="bob")
    await wired.store.add(wired.fake, mine, idle_ttl=86400)
    await wired.store.add(wired.fake, other, idle_ttl=86400)
    with _identity(user_id="keyA", owner="alice"):
        page = await ops.list_interactions()
    assert [item["interaction_id"] for item in page["items"]] == ["mine"]
    assert page["total"] == 1  # the audience filter runs BEFORE paging — an honest total
    assert page["items"][0]["media"] == _EXPECTED_MEDIA_FRAME


async def test_list_media_frame_broadcast(wired):
    # An unrestricted operator sees a broadcast (unaddressed) media question, media intact.
    broadcast = _media_request(wired.store, iid="cast", gid="gc", media=_MEDIA_ITEMS, audience=None)
    await wired.store.add(wired.fake, broadcast, idle_ttl=86400)
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert [item["interaction_id"] for item in page["items"]] == ["cast"]
    assert page["items"][0]["media"] == _EXPECTED_MEDIA_FRAME


async def test_list_restricted_shows_only_addressed(wired):
    await _seed_addressed(wired, "a1", "ga", "keyA")
    await _seed_addressed(wired, "b1", "gb", "bob")
    await _seed_addressed(wired, "own1", "gown", "alice")  # addressed to keyA's OWNER
    await _seed_addressed(wired, "o1", "go", None)  # broadcast/operator
    with _identity(user_id="keyA", owner="alice"):
        page = await ops.list_interactions()
    # Only keyA's OWN-addressed question — not bob's, not the unaddressed broadcast, and
    # NOT the one addressed to its owner "alice" (key-keyed, not owner-keyed).
    assert [item["interaction_id"] for item in page["items"]] == ["a1"]
    assert page["total"] == 1  # the audience filter runs BEFORE paging


async def test_list_unrestricted_shows_all(wired):
    await _seed_addressed(wired, "a1", "ga", "alice")
    await _seed_addressed(wired, "o1", "go", None)
    # An unrestricted (authenticated, no owner) caller keeps today's full inbox.
    with _identity(user_id="op1", owner=None):
        page = await ops.list_interactions()
    assert sorted(item["interaction_id"] for item in page["items"]) == ["a1", "o1"]


async def test_pending_operator_gets_items_and_count(wired):
    park = _async_park(wired.store, iid="p1", channel="telegram", recipient="@ops")
    await wired.store.add(wired.fake, park, idle_ttl=86400)
    with _identity(user_id="op1", owner=None):
        result = await ops.list_pending_interactions()
    assert result["count"] == 1
    assert result["items"][0]["interaction_id"] == "p1"
    assert result["items"][0]["channel"] == "telegram"
    assert result["items"][0]["recipient"] == "@ops"


async def test_pending_restricted_caller_sees_only_its_addressed_slice(wired):
    # A restricted caller gets the inbox's audience filter, never a 403 — this
    # operation is PROJECTED, and a projected route must agree with the gate for
    # every identity it is projected to (the owned-keys e2e pins that invariant).
    await wired.store.add(wired.fake, _async_park(wired.store, iid="mine", audience="u1"), idle_ttl=86400)
    await wired.store.add(
        wired.fake, _async_park(wired.store, iid="other", gid="og", audience="owner-2"), idle_ttl=86400
    )
    await wired.store.add(wired.fake, _async_park(wired.store, iid="broadcast", gid="bg"), idle_ttl=86400)
    with _identity(user_id="u1", owner="owner-1"):
        body = await ops.list_pending_interactions()
    assert body["count"] == 1
    assert [item["interaction_id"] for item in body["items"]] == ["mine"]


async def test_pending_route_returns_data_envelope(wired):
    await wired.store.add(wired.fake, _async_park(wired.store, iid="p1"), idle_ttl=86400)
    resp = await router.list_pending_interactions(make_request("GET"))
    assert resp.status_code == 200
    body = _json(resp)["data"]
    assert body["count"] == 1
    assert body["items"][0]["interaction_id"] == "p1"


async def test_pending_route_restricted_gets_filtered_200(wired):
    # Through the route a restricted caller is served (200) with only its own
    # addressed slice — the projected-route/gate agreement, not a denial.
    await wired.store.add(wired.fake, _async_park(wired.store, iid="mine", audience="u1"), idle_ttl=86400)
    await wired.store.add(
        wired.fake, _async_park(wired.store, iid="other", gid="og", audience="owner-2"), idle_ttl=86400
    )
    with _identity(user_id="u1", owner="owner-1"):
        resp = await router.list_pending_interactions(make_request("GET"))
    assert resp.status_code == 200
    body = _json(resp)["data"]
    assert body["count"] == 1
    assert body["items"][0]["interaction_id"] == "mine"


async def test_pending_route_clamps_limit(wired, monkeypatch):
    captured: dict[str, int] = {}
    real = InteractionStore.list_pending

    async def spy(self, r, *, now, limit):
        captured["limit"] = limit
        return await real(self, r, now=now, limit=limit)

    monkeypatch.setattr(InteractionStore, "list_pending", spy)
    # Above the cap clamps down; below 1 clamps up — never refused.
    high = await router.list_pending_interactions(make_request("GET", query="limit=5000"))
    assert high.status_code == 200
    assert captured["limit"] == 1000
    low = await router.list_pending_interactions(make_request("GET", query="limit=0"))
    assert low.status_code == 200
    assert captured["limit"] == 1


async def test_pending_route_non_integer_limit_400(wired):
    resp = await router.list_pending_interactions(make_request("GET", query="limit=abc"))
    assert resp.status_code == 400


async def test_pending_route_default_limit_500(wired, monkeypatch):
    captured: dict[str, int] = {}

    async def spy(self, r, *, now, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(InteractionStore, "list_pending", spy)
    resp = await router.list_pending_interactions(make_request("GET"))
    assert resp.status_code == 200
    assert captured["limit"] == 500
