"""Interactions human answer door and the answer-door audience-isolation matrix."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.routers import interactions as router

from ._harness import _identity, _json, _plain_request, _seed, _seed_addressed, make_request


async def test_answer_unknown_interaction_404(wired):
    req = make_request("POST", path_params={"interaction_id": "ghost"}, body=b'{"answer":"hi"}')
    resp = await router.answer(req)
    assert resp.status_code == 404


async def test_answer_external_rejected_400(wired):
    await _seed(wired)
    req = make_request("POST", path_params={"interaction_id": "i1"}, body=b'{"answer":"x"}')
    resp = await router.answer(req)
    assert resp.status_code == 400
    assert "callback URL" in _json(resp)["error"]


async def test_answer_text_then_second_is_409(wired):
    now = datetime.now(UTC)
    req_model = InteractionRequest(
        interaction_id="t1",
        group_id="g",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("t1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )
    await wired.store.add(wired.fake, req_model, idle_ttl=86400)

    first = await router.answer(make_request("POST", path_params={"interaction_id": "t1"}, body=b'{"answer":"hello"}'))
    assert first.status_code == 200
    second = await router.answer(make_request("POST", path_params={"interaction_id": "t1"}, body=b'{"answer":"again"}'))
    assert second.status_code == 409


async def test_answer_oversized_body_413(wired):
    wired.settings.callback_max_body_bytes = 10
    req = make_request("POST", path_params={"interaction_id": "ghost"}, body=b"x" * 100)
    resp = await router.answer(req)
    assert resp.status_code == 413


async def test_answer_invalid_value_400(wired):
    now = datetime.now(UTC)
    req_model = InteractionRequest(
        interaction_id="c1",
        group_id="g",
        question="?",
        answer_format=AnswerFormat.CONFIRM,
        reply_to=wired.store.reply_key("c1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )
    await wired.store.add(wired.fake, req_model, idle_ttl=86400)
    req = make_request("POST", path_params={"interaction_id": "c1"}, body=b'{"answer":"not-a-bool"}')
    resp = await router.answer(req)
    assert resp.status_code == 400


async def test_answer_text_non_string_400(wired):
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":123}'))
    assert resp.status_code == 400


async def test_answer_select_valid_and_invalid(wired):
    req = _plain_request(wired.store, AnswerFormat.SELECT, payload={"options": ["a", "b"]})
    await wired.store.add(wired.fake, req, idle_ttl=86400)
    bad = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":"c"}'))
    assert bad.status_code == 400
    good = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":"a"}'))
    assert good.status_code == 200


async def test_answer_form_branches(wired):
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    req = _plain_request(wired.store, AnswerFormat.FORM, payload={"schema": schema})
    await wired.store.add(wired.fake, req, idle_ttl=86400)
    non_dict = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":"str"}'))
    assert non_dict.status_code == 400
    bad = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":{"y":1}}'))
    assert bad.status_code == 400
    ok = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":{"x":1}}'))
    assert ok.status_code == 200


async def test_answer_confirm_valid(wired):
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.CONFIRM), idle_ttl=86400)
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":true}'))
    assert resp.status_code == 200


async def test_answer_form_malformed_stored_schema_400(wired):
    # A stored form question whose schema is truthy-but-not-a-dict is rejected
    # loudly rather than waving the answer through.
    req = _plain_request(wired.store, AnswerFormat.FORM, payload={"schema": "not-a-dict"})
    await wired.store.add(wired.fake, req, idle_ttl=86400)
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":{"x":1}}'))
    assert resp.status_code == 400


async def test_answer_lost_race_is_409(wired, monkeypatch):
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.TEXT), idle_ttl=86400)

    async def _lose(*args, **kwargs):
        return False

    monkeypatch.setattr(InteractionStore, "record_answer", _lose)
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":"hi"}'))
    assert resp.status_code == 409


async def test_answer_deeply_nested_object_400_not_500(wired):
    # The human door shares the callback door's serialization guard: a FORM answer
    # that passes a permissive schema but blows the serializer must 400, not 500.
    schema = {"type": "object"}  # permissive: validates any object in O(1)
    req = _plain_request(wired.store, AnswerFormat.FORM, payload={"schema": schema})
    await wired.store.add(wired.fake, req, idle_ttl=86400)
    nested: dict = {}
    cur = nested
    for _ in range(3000):
        cur["a"] = {}
        cur = cur["a"]
    body = json.dumps({"answer": nested}).encode()
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=body))
    assert resp.status_code == 400


async def test_answer_invalid_json_body_400(wired):
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b"not json"))
    assert resp.status_code == 400
    assert _json(resp)["error"] == "invalid JSON body"


async def test_answer_missing_answer_key_400(wired):
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b"{}"))
    assert resp.status_code == 400


async def test_audience_holder_answers_addressed_200(wired):
    await _seed_addressed(wired, "ad", "gad", "keyA")
    with _identity(user_id="keyA", owner="alice"):
        resp = await router.answer(make_request("POST", path_params={"interaction_id": "ad"}, body=b'{"answer":"x"}'))
    assert resp.status_code == 200


async def test_other_restricted_answers_addressed_403(wired):
    # Addressed to keyA; a DIFFERENT key keyB (even were it same-owner) is denied.
    await _seed_addressed(wired, "ad", "gad", "keyA")
    with _identity(user_id="keyB", owner="bob"):
        resp = await router.answer(make_request("POST", path_params={"interaction_id": "ad"}, body=b'{"answer":"x"}'))
    assert resp.status_code == 403
    assert _json(resp)["error"] == "interaction is addressed to another identity"


async def test_unrestricted_answers_addressed_200(wired):
    # The operator can always unblock a stuck addressed question.
    await _seed_addressed(wired, "ad", "gad", "alice")
    with _identity(user_id="op1", owner=None):
        resp = await router.answer(make_request("POST", path_params={"interaction_id": "ad"}, body=b'{"answer":"x"}'))
    assert resp.status_code == 200


async def test_unrestricted_answers_unaddressed_200(wired):
    await _seed_addressed(wired, "un", "gun", None)
    with _identity(user_id="op1", owner=None):
        resp = await router.answer(make_request("POST", path_params={"interaction_id": "un"}, body=b'{"answer":"x"}'))
    assert resp.status_code == 200


async def test_restricted_answers_unaddressed_403(wired):
    await _seed_addressed(wired, "un", "gun", None)
    with _identity(user_id="keyA", owner="alice"):
        resp = await router.answer(make_request("POST", path_params={"interaction_id": "un"}, body=b'{"answer":"x"}'))
    assert resp.status_code == 403
    assert _json(resp)["error"] == "restricted identities may answer only interactions addressed to them"


async def test_answer_no_auth_records_namespaced_sentinel(wired):
    # With access control disabled no caller identity is bound; the recorded
    # ``answered_by`` is the reserved namespaced sentinel, not a bare "anonymous"
    # that could collide with a real user id.
    await wired.store.add(wired.fake, _plain_request(wired.store, AnswerFormat.TEXT), idle_ttl=86400)
    resp = await router.answer(make_request("POST", path_params={"interaction_id": "p1"}, body=b'{"answer":"hi"}'))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, "p1")
    assert state is not None
    assert state.response is not None
    assert state.response.answered_by == "system:no-auth"
    assert state.response.answered_by == ops._NO_AUTH_ANSWERED_BY
    # The sentinel is namespaced (``system:`` prefix), so it can't be a real id.
    assert ops._NO_AUTH_ANSWERED_BY.startswith("system:")
