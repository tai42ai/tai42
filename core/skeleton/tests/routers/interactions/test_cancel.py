"""Interactions cancel door: withdraw a pending ask without answering or deleting its thread."""

from __future__ import annotations

from tai42_contract.interactions import AnswerFormat

from tai42_skeleton.routers import interactions as router

from ._harness import _identity, _json, _plain_request, _seed_addressed, make_request


async def test_cancel_pending_200_and_tags_removed_event(wired):
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    resp = await router.cancel(make_request("POST", path_params={"interaction_id": "p1"}))
    assert resp.status_code == 200
    assert _json(resp)["data"] == {"interaction_id": "p1", "status": "cancelled"}
    # Gone from pending, and the removed event rides tagged ``cancelled``.
    assert await wired.store.get_state(wired.fake, "p1") is None
    events = await wired.fake.xrange(wired.store.events_key)
    removed = [f for _id, f in events if f["type"] == "interaction.removed"]
    assert removed
    assert removed[-1]["reason"] == "cancelled"


async def test_cancel_unknown_interaction_404(wired):
    resp = await router.cancel(make_request("POST", path_params={"interaction_id": "ghost"}))
    assert resp.status_code == 404


async def test_cancel_answered_is_409(wired):
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    first = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":"x"}'))
    assert first.status_code == 200
    resp = await router.cancel(make_request("POST", path_params={"interaction_id": "p1"}))
    assert resp.status_code == 409


async def test_cancel_recancel_is_404(wired):
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    assert (await router.cancel(make_request("POST", path_params={"interaction_id": "p1"}))).status_code == 200
    assert (await router.cancel(make_request("POST", path_params={"interaction_id": "p1"}))).status_code == 404


async def test_cancel_audience_holder_200(wired):
    await _seed_addressed(wired, "ad", "gad", "keyA")
    with _identity(user_id="keyA", owner="alice"):
        resp = await router.cancel(make_request("POST", path_params={"interaction_id": "ad"}))
    assert resp.status_code == 200


async def test_cancel_other_restricted_403(wired):
    await _seed_addressed(wired, "ad", "gad", "keyA")
    with _identity(user_id="keyB", owner="bob"):
        resp = await router.cancel(make_request("POST", path_params={"interaction_id": "ad"}))
    assert resp.status_code == 403
    assert _json(resp)["error"] == "interaction is addressed to another identity"


async def test_cancel_unrestricted_addressed_200(wired):
    await _seed_addressed(wired, "ad", "gad", "alice")
    with _identity(user_id="op1", owner=None):
        resp = await router.cancel(make_request("POST", path_params={"interaction_id": "ad"}))
    assert resp.status_code == 200


async def test_cancel_restricted_unaddressed_403(wired):
    await _seed_addressed(wired, "un", "gun", None)
    with _identity(user_id="keyA", owner="alice"):
        resp = await router.cancel(make_request("POST", path_params={"interaction_id": "un"}))
    assert resp.status_code == 403
    assert _json(resp)["error"] == "restricted identities may cancel only interactions addressed to them"
