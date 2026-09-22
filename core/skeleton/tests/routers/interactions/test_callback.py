"""Interactions callback doors: the POST data claim, the GET redirect/confirm/reply pages,
webhook-verifier binding, channel-delivered typed formats, and the store-unconfigured OFF gate."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionResponse

from tai42_skeleton.interactions import InteractionStore, ask
from tai42_skeleton.interactions.caller_ask import CALLER_ASK_RESOLUTION_REFUSED
from tai42_skeleton.operations.interactions import _add_data, _reply_ttl
from tai42_skeleton.routers import interactions as router
from tai42_skeleton.routers.interactions.callback import _POST_ONLY_EMPTY_BODY_DENY
from tai42_skeleton.routers.interactions.pages import _CONFIRM_PAGE, _REPLY_PAGE

from ..._helpers import await_add_event
from ._harness import (
    _BINDING,
    _answer_bound,
    _caller_ask_request,
    _external_request,
    _FakeVerifier,
    _json,
    _off,
    _plain_request,
    _seed,
    _seed_form,
    _seed_form_payload,
    _seed_typed,
    _typed_request,
    make_request,
)


async def test_post_caller_ask_typed_refused_409(wired):
    # A caller ask reaching the public answer door through a ticket (defence-in-depth:
    # a caller ask carries no ticket) is refused loudly before the typed claim branch —
    # it is answerable only by its calling run.
    await wired.store.add(
        wired.fake, _caller_ask_request(wired.store, fmt=AnswerFormat.TEXT), idle_ttl=86400, ticket="TKT", ticket_ttl=60
    )
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": "x"}'))
    assert resp.status_code == 409
    assert _json(resp)["error"] == CALLER_ASK_RESOLUTION_REFUSED


async def test_post_caller_ask_external_refused_409(wired):
    # The same refusal pre-empts the external claim branch: the shared seam runs before
    # the door splits on answer_format, so an EXTERNAL-format caller ask is refused too.
    await wired.store.add(
        wired.fake,
        _caller_ask_request(wired.store, fmt=AnswerFormat.EXTERNAL, payload={"url": "https://ext.example/r"}),
        idle_ttl=86400,
        ticket="TKT",
        ticket_ttl=60,
    )
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"approved": true}'))
    assert resp.status_code == 409
    assert _json(resp)["error"] == CALLER_ASK_RESOLUTION_REFUSED


async def test_post_valid_body_wakes_caller(wired):
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", timeout=5))
    iid, _gid = await await_add_event(wired.fake, wired.store)

    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"signed":true}'))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "answered"

    result = await task
    assert result == {"signed": True}
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.answered_by == "external-callback"


async def test_post_form_answer_schema_mismatch_400_carries_field(wired):
    # The callback door's form-answer 400 body carries the failing field's dotted
    # path as an optional ``field`` key (the same string the message embeds) so a
    # channel can pin the error on the right control.
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    await _seed_form(wired, schema=schema)
    resp = await router.callback(
        make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": {"count": "abc"}}')
    )
    assert resp.status_code == 400
    body = _json(resp)
    assert body["error"] == "answer does not match schema at count: 'abc' is not of type 'integer'"
    assert body["field"] == "count"
    # The door signals a correlated channel that this rejection is re-answerable in
    # place — the live ask stands, so the participant can answer again.
    assert body["retry_in_place"] is True


async def test_post_form_answer_root_mismatch_400_omits_field(wired):
    # A root-level mismatch (a missing required field) has no answer-field location,
    # so the 400 body carries NO ``field`` key.
    schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}
    await _seed_form(wired, schema=schema)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": {}}'))
    assert resp.status_code == 400
    body = _json(resp)
    assert body["error"] == "answer does not match schema: 'count' is a required property"
    assert "field" not in body
    # The retry-in-place policy signal is present even when the failing field is
    # unlocated (no ``field`` key).
    assert body["retry_in_place"] is True


async def test_post_unknown_ticket_404(wired):
    resp = await router.callback(make_request("POST", path_params={"ticket": "NOPE"}, body=b"{}"))
    assert resp.status_code == 404
    assert _json(resp) == {"error": "not found"}
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_post_expired_ticket_404_identical(wired):
    await _seed(wired, budget=60)
    wired.fake.advance(61)  # ticket TTL elapsed
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"{}"))
    assert resp.status_code == 404
    assert _json(resp) == {"error": "not found"}


async def test_post_state_missing_after_resolve_404(wired):
    await _seed(wired)
    # A cancel/timeout prune deletes state while the ticket lives out its TTL.
    await wired.store.prune_pending(wired.fake, "i1", "g1")
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"{}"))
    assert resp.status_code == 404
    assert _json(resp) == {"error": "not found"}


async def test_post_answered_idempotent_200_and_ticket_survives(wired):
    await _seed(wired)
    prior = InteractionResponse(
        interaction_id="i1", answer={"x": 1}, answered_by="external-callback", answered_at=datetime.now(UTC)
    )
    await wired.store.record_answer(wired.fake, prior, "g1", reply_ttl=60, ticket="TKT", ticket_ttl=86400)

    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"y":2}'))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "already_answered"
    # The ticket still resolves (never deleted).
    assert await wired.store.resolve_ticket(wired.fake, "TKT") == "i1"


@pytest.mark.parametrize("body", [b"[1,2]", b'"scalar"', b"not json at all"])
async def test_post_non_object_body_400(wired, body):
    await _seed(wired)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=body))
    assert resp.status_code == 400
    # Caller stays blocked: the interaction is still pending.
    state = await wired.store.get_state(wired.fake, "i1")
    assert state is not None
    assert state.status == "pending"


async def test_post_schema_invalid_then_valid(wired):
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", schema=schema, timeout=5))
    await await_add_event(wired.fake, wired.store)

    bad = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"nope":1}'))
    assert bad.status_code == 400
    assert not task.done()

    good = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"x":7}'))
    assert good.status_code == 200
    assert await task == {"x": 7}


async def test_post_oversized_body_413(wired):
    wired.settings.callback_max_body_bytes = 10
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x" * 100))
    assert resp.status_code == 413


async def test_post_oversized_query_413(wired):
    wired.settings.callback_max_body_bytes = 10
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="a=" + "z" * 100))
    assert resp.status_code == 413


async def test_post_large_body_small_content_length_413(wired):
    wired.settings.callback_max_body_bytes = 10
    # A lying/absent Content-Length must not bypass the ACTUAL-byte cap.
    resp = await router.callback(
        make_request("POST", path_params={"ticket": "TKT"}, body=b"x" * 100, headers={"content-length": "2"})
    )
    assert resp.status_code == 413


async def test_post_empty_body_uses_query_params(wired):
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", timeout=5))
    await await_add_event(wired.fake, wired.store)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="a=1&tag=x&tag=y"))
    assert resp.status_code == 200
    assert await task == {"a": "1", "tag": ["x", "y"]}


async def test_post_body_wins_over_query(wired):
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", timeout=5))
    await await_add_event(wired.fake, wired.store)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="a=1", body=b'{"b":2}'))
    assert resp.status_code == 200
    assert await task == {"b": 2}


async def test_post_deeply_nested_body_400_not_500(wired):
    # A deeply-nested JSON OBJECT (not an array) isolates the PARSE-step recursion
    # catch: it would parse to a dict if json.loads didn't blow the recursion limit
    # first, so it can't 400 via the non-object guard — only via the parse except.
    await _seed(wired)
    body = (b'{"a":' * 6000) + b"1" + (b"}" * 6000)  # under the 64KiB cap
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=body))
    assert resp.status_code == 400


async def test_post_recursive_schema_400_not_500(wired):
    # A self-referential schema recurses to the body's depth; a deeply-nested body
    # under the size cap must yield 400, not a 500 from the validator blowup.
    schema = {"type": "object", "properties": {"a": {"$ref": "#"}}}
    await _seed(wired, schema=schema)
    nested: dict = {}
    cur = nested
    for _ in range(3000):
        cur["a"] = {}
        cur = cur["a"]
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=json.dumps(nested).encode()))
    assert resp.status_code == 400


async def test_post_json_headers_nosniff_nostore(wired):
    await _seed(wired)
    schema = {"type": "object", "required": ["x"]}
    req2 = _external_request(wired.store, "i2", "g2", schema)
    await wired.store.add(wired.fake, req2, idle_ttl=86400, ticket="TK2", ticket_ttl=60)
    for ticket, body, expect in [("TKT", b"{}", 200), ("TK2", b"{}", 400), ("NONE", b"{}", 404)]:
        resp = await router.callback(make_request("POST", path_params={"ticket": ticket}, body=body))
        assert resp.status_code == expect
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-content-type-options"] == "nosniff"


async def test_get_pending_confirm_page_no_state_change(wired):
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", timeout=5))
    await await_add_event(wired.fake, wired.store)

    for _ in range(2):
        resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
        assert resp.status_code == 200
        assert b'<form method="post">' in bytes(resp.body)
        assert not task.done()  # GET never mutates

    # Finish so the task doesn't dangle.
    await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"ok":1}'))
    assert await task == {"ok": 1}


async def test_get_answered_done_page(wired):
    await _seed(wired)
    prior = InteractionResponse(
        interaction_id="i1", answer={}, answered_by="external-callback", answered_at=datetime.now(UTC)
    )
    await wired.store.record_answer(wired.fake, prior, "g1", reply_ttl=60, ticket="TKT", ticket_ttl=86400)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    assert b"already been answered" in bytes(resp.body)


async def test_post_form_answer_honors_per_send_options(wired):
    # A per-send option value REPLACES the schema enum for this send, so a value outside
    # the published enum but inside the per-send list is accepted; one outside both is 400.
    schema = {"type": "object", "properties": {"color": {"type": "string", "enum": ["red", "blue"]}}}
    data = {"options": {"color": [{"value": "green"}, {"value": "amber"}]}}
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    ok = await router.callback(
        make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": {"color": "green"}}')
    )
    assert ok.status_code == 200
    assert _json(ok)["data"]["status"] == "answered"


async def test_post_form_answer_rejects_value_outside_per_send_options(wired):
    schema = {"type": "object", "properties": {"color": {"type": "string", "enum": ["red", "blue"]}}}
    data = {"options": {"color": [{"value": "green"}, {"value": "amber"}]}}
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    # ``red`` is in the PUBLISHED enum but not the per-send list — rejected.
    resp = await router.callback(
        make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": {"color": "red"}}')
    )
    assert resp.status_code == 400
    assert "does not match schema" in _json(resp)["error"]


async def test_get_answered_form_ticket_returns_done_page(wired):
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    await _seed_form(wired, schema=schema)
    prior = InteractionResponse(
        interaction_id="i1", answer={"x": 1}, answered_by="external-callback", answered_at=datetime.now(UTC)
    )
    await wired.store.record_answer(wired.fake, prior, "g1", reply_ttl=60, ticket="TKT", ticket_ttl=86400)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    assert b"already been answered" in bytes(resp.body)


async def test_get_form_ticket_malformed_schema_500(wired):
    # A form record whose stored schema is not a dict cannot be rendered — a server
    # bug that answers a loud 500, never a blank page.
    await _seed_form(wired, schema="not-a-dict")
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 500


async def test_get_form_ticket_unsupported_property_type_500(wired):
    # A property type outside the supported subset is a loud 500 (logged reason),
    # never a half-rendered form silently dropping the field.
    schema = {"type": "object", "properties": {"blob": {"type": "array"}}}
    await _seed_form(wired, schema=schema)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 500


async def test_get_unknown_ticket_plain_404(wired):
    resp = await router.callback(make_request("GET", path_params={"ticket": "NOPE"}))
    assert resp.status_code == 404
    assert b"<form" not in bytes(resp.body)
    assert bytes(resp.body) == b"Not Found"


async def test_get_state_missing_plain_404(wired):
    await _seed(wired)
    await wired.store.prune_pending(wired.fake, "i1", "g1")
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 404
    assert b"<form" not in bytes(resp.body)
    assert bytes(resp.body) == b"Not Found"


async def test_get_html_is_byte_constant_and_headers(wired):
    await _seed(wired)
    resp = await router.callback(
        make_request("GET", path_params={"ticket": "TKT"}, query='x=<script>alert(1)</script>&y="><b>')
    )
    body = bytes(resp.body)
    assert b"<script>" not in body
    assert b'"><b>' not in body
    # The page is byte-for-byte the module constant — no request-derived value
    # (query, ticket, or path) is interpolated anywhere.
    assert body == _CONFIRM_PAGE.encode()
    assert resp.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


async def test_get_single_and_multi_query_values(wired):
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", timeout=5))
    await await_add_event(wired.fake, wired.store)
    # The confirm form POSTs back to the same URL with the query string.
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="a=1&tag=a&tag=b"))
    assert resp.status_code == 200
    assert await task == {"a": "1", "tag": ["a", "b"]}


async def test_post_lost_race_is_already_answered(wired, monkeypatch):
    await _seed(wired)

    async def _lose(*args, **kwargs):
        return False

    monkeypatch.setattr(InteractionStore, "record_answer", _lose)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"ok":1}'))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "already_answered"


async def test_post_three_repeated_query_values(wired):
    task = asyncio.create_task(ask("Sign?", answer_format="external", link="{callback_url}", timeout=5))
    await await_add_event(wired.fake, wired.store)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="tag=a&tag=b&tag=c"))
    assert resp.status_code == 200
    assert await task == {"tag": ["a", "b", "c"]}


def test_reply_ttl_clamps_past_deadline(wired):
    # A question already past its deadline still gets a positive reply TTL (the
    # 1s floor) so the RPUSH/EXPIRE pair stays valid and the key dies at once.
    req = _external_request(wired.store, budget=-30)
    assert _reply_ttl(req) == 1


async def test_callback_answers_addressed_external_regardless_of_audience(wired):
    # The unauth ticket callback is NOT audience-gated: the ticket is the capability.
    req = _external_request(wired.store, iid="ext", gid="gext").model_copy(update={"audience": "alice"})
    await wired.store.add(wired.fake, req, idle_ttl=86400, ticket="TKT", ticket_ttl=60)
    # No caller identity bound — an external answerer holds only the ticket.
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"approved":true}'))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "answered"


async def test_callback_bound_verifies_before_record(wired, verifier_registry):
    order: list[str] = []
    verifier_registry.register("prov", _FakeVerifier(order=order))
    await _seed(wired, verifier=_BINDING)

    # The route builds its own store instance, so spy on the class method: the
    # ticket/state reads share the fake redis, only record_answer is order-tracked.
    orig_record = InteractionStore.record_answer

    async def spy_record(self, *a, **k):
        order.append("record")
        return await orig_record(self, *a, **k)

    wired.monkeypatch.setattr(InteractionStore, "record_answer", spy_record)

    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"signed":true}'))
    assert resp.status_code == 200
    # Verify ran, and BEFORE the answer was recorded.
    assert order == ["verify", "record"]


async def test_callback_bound_verify_failure_401_ticket_unconsumed(wired, verifier_registry):
    from tai42_contract.webhooks import WebhookVerificationError

    verifier_registry.register("prov", _FakeVerifier(raise_exc=WebhookVerificationError("bad sig")))
    iid = await _seed(wired, verifier=_BINDING)

    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x"))
    assert resp.status_code == 401
    assert _json(resp)["error"] == "webhook verification failed"
    # Nothing recorded — the question stays pending.
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.status == "pending"
    # The ticket is NOT consumed — it still resolves, so a legitimate retry works.
    assert await wired.store.resolve_ticket(wired.fake, "TKT") == iid


async def test_callback_bound_missing_verifier_500(wired, verifier_registry):
    # Binding names a verifier that is not registered -> fail closed 500.
    await _seed(wired, verifier={"name": "ghost", "config": {}})
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x"))
    assert resp.status_code == 500


async def test_callback_get_bound_question_no_confirm_page(wired, verifier_registry):
    verifier_registry.register("prov", _FakeVerifier())
    await _seed(wired, verifier=_BINDING)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    # A verifier-bound question is server-to-server only: no confirm page, and the
    # response is the same constant 404 body an unknown ticket answers.
    assert resp.status_code == 404
    assert b"<form" not in bytes(resp.body)
    assert bytes(resp.body) == b"Not Found"


async def test_callback_get_bound_answered_is_404_not_done_page(wired, verifier_registry):
    # A bound question that has been ANSWERED must still 404 with the constant
    # body — never the 200 done page, which would reveal the answered state.
    verifier_registry.register("prov", _FakeVerifier())
    await _seed(wired, verifier=_BINDING)
    await _answer_bound(wired)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 404
    assert bytes(resp.body) == b"Not Found"
    assert b"already been answered" not in bytes(resp.body)


async def test_callback_get_bound_states_indistinguishable(wired, verifier_registry):
    # The property: for a verifier-bound question a GET on an unknown ticket, a
    # LIVE ticket, and an ANSWERED ticket are byte-identical — status, body, and
    # the two _BASE_HEADERS. A ticket holder learns nothing about the state.
    verifier_registry.register("prov", _FakeVerifier())

    unknown = await router.callback(make_request("GET", path_params={"ticket": "NOPE"}))

    await _seed(wired, verifier=_BINDING)
    live = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))

    await _answer_bound(wired)
    answered = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))

    for other in (live, answered):
        assert other.status_code == unknown.status_code
        assert bytes(other.body) == bytes(unknown.body)
        assert other.headers["cache-control"] == unknown.headers["cache-control"]
        assert other.headers["x-content-type-options"] == unknown.headers["x-content-type-options"]


async def test_callback_post_bound_answered_wrong_secret_is_401(wired, verifier_registry):
    # A wrong bridge secret on a bound + answered POST returns 401 from the
    # verifier, identical to a live one — the answered state is not revealed.
    from tai42_contract.webhooks import WebhookVerificationError

    verifier_registry.register("prov", _FakeVerifier(raise_exc=WebhookVerificationError("bad sig")))
    await _seed(wired, verifier=_BINDING)
    await _answer_bound(wired)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x"))
    assert resp.status_code == 401
    assert _json(resp)["error"] == "webhook verification failed"


async def test_callback_post_bound_answered_correct_secret_is_idempotent_200(wired, verifier_registry):
    # The genuine provider-retry case: a bound + answered POST that PASSES
    # verification still gets the idempotent 200, and nothing is re-recorded.
    verifier_registry.register("prov", _FakeVerifier())  # accepts
    await _seed(wired, verifier=_BINDING)
    await _answer_bound(wired, answer={"x": 1})

    orig_record = InteractionStore.record_answer
    recorded: list = []

    async def spy_record(self, *a, **k):
        recorded.append(True)
        return await orig_record(self, *a, **k)

    wired.monkeypatch.setattr(InteractionStore, "record_answer", spy_record)

    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"signed":true}'))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "already_answered"
    # The recorded answer is untouched: the answered branch returns without
    # re-recording.
    assert recorded == []


async def test_callback_post_bound_live_and_answered_indistinguishable(wired, verifier_registry):
    # The property: the SAME failing-verification POST at a live bound question
    # and at an answered one yields byte-identical responses (status + body).
    from tai42_contract.webhooks import WebhookVerificationError

    verifier_registry.register("prov", _FakeVerifier(raise_exc=WebhookVerificationError("bad sig")))

    await _seed(wired, verifier=_BINDING)
    live = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x"))

    await _answer_bound(wired)
    answered = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x"))

    assert live.status_code == answered.status_code
    assert bytes(live.body) == bytes(answered.body)


async def test_callback_post_bound_answered_missing_verifier_500(wired, verifier_registry):
    # A bound + answered POST whose verifier cannot be constructed (name
    # unregistered) 500s because verification runs before the answered check —
    # a door that cannot authenticate must not report state.
    await _seed(wired, verifier={"name": "ghost", "config": {}})
    await _answer_bound(wired)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"x"))
    assert resp.status_code == 500
    assert _json(resp)["error"] == "webhook verification error"


async def test_unbound_external_callback_unchanged(wired):
    # No verifier binding -> today's ticket-only behavior, GET serves the confirm.
    await _seed(wired)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    assert bytes(resp.body) == _CONFIRM_PAGE.encode()


async def test_callback_post_only_empty_body_query_answer_denied(wired, verifier_registry):
    # A body-signature (post_only) verifier signs only the body. An empty-body POST
    # with a query-string answer must be DENIED — a replayed signature over an empty
    # body must never let ``?approved=true`` inject an answer.
    verifier_registry.register("prov", _FakeVerifier())  # post_only=True
    iid = await _seed(wired, verifier=_BINDING)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="approved=true"))
    assert resp.status_code == 400
    assert _json(resp)["error"] == _POST_ONLY_EMPTY_BODY_DENY
    # Nothing recorded: the question stays pending, the ticket is unconsumed.
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.status == "pending"
    assert await wired.store.resolve_ticket(wired.fake, "TKT") == iid


async def test_callback_non_post_only_empty_body_query_answer_accepted(wired, verifier_registry):
    # A header-signature (post_only=False) verifier keeps the empty-body/query path:
    # the verifier passes over the empty body and the query answer is accepted.
    header_verifier = _FakeVerifier()
    header_verifier.post_only = False
    verifier_registry.register("prov", header_verifier)
    iid = await _seed(wired, verifier=_BINDING)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, query="approved=true"))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.status == "answered"
    assert state.response is not None
    assert state.response.answer == {"approved": "true"}


async def test_callback_post_only_signed_body_accepted(wired, verifier_registry):
    # A passing post_only verifier over a NON-empty (signed) body is accepted — the
    # answer rides the signed body, so post_only only blocks the empty-body path.
    verifier_registry.register("prov", _FakeVerifier())  # post_only=True
    iid = await _seed(wired, verifier=_BINDING)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"signed":true}'))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.status == "answered"
    assert state.response is not None
    assert state.response.answer == {"signed": True}


def test_add_frame_strips_verifier_and_flags_server_verified(wired):
    req = _external_request(wired.store, "iv", "gv", verifier=_BINDING)
    data = _add_data(req)
    # The verifier config is stripped from the client frame...
    assert "verifier" not in (data["format_payload"] or {})
    # ...and replaced with a server_verified flag.
    assert data["server_verified"] is True
    # A non-verified external question carries no server_verified key.
    plain = _add_data(_external_request(wired.store, "ip", "gp"))
    assert "server_verified" not in plain


async def test_callback_text_records_typed_str(wired):
    iid = await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": "yes"}'))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "answered"
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.answer == "yes"  # the TYPED str, not the envelope dict


async def test_callback_records_answer_params(wired):
    # The inbound-answer ladder may forward channel enrichment beside the answer; the door
    # validates it against the shared entry-param bounds and records it onto the response's
    # params, the answer-seam counterpart of a bridged turn's entry params.
    iid = await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(
        make_request(
            "POST",
            path_params={"ticket": "TKT"},
            body=b'{"answer": "yes", "params": {"reply_id": "wamid.X"}}',
        )
    )
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.answer == "yes"
    assert state.response.params == {"reply_id": "wamid.X"}


async def test_callback_without_params_records_none(wired):
    # Absent params keep the envelope byte-identical to a plain answer.
    iid = await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": "yes"}'))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.params is None


async def test_callback_rejects_invalid_params(wired):
    # A params object over the shared transport bounds is refused with a 400 before the answer
    # is recorded — the caller stays blocked.
    await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(
        make_request(
            "POST",
            path_params={"ticket": "TKT"},
            body=b'{"answer": "yes", "params": {"bad key": "x"}}',
        )
    )
    assert resp.status_code == 400
    state = await wired.store.get_state(wired.fake, "t1")
    assert state is not None
    assert state.status == "pending"


async def test_callback_rejects_non_object_params(wired):
    await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(
        make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": "yes", "params": ["nope"]}')
    )
    assert resp.status_code == 400
    state = await wired.store.get_state(wired.fake, "t1")
    assert state is not None
    assert state.status == "pending"


async def test_callback_confirm_records_typed_bool(wired):
    iid = await _seed_typed(wired, AnswerFormat.CONFIRM)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": false}'))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.answer is False


async def test_callback_select_records_chosen_option(wired):
    iid = await _seed_typed(wired, AnswerFormat.SELECT, options=["red", "blue"])
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": "blue"}'))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.answer == "blue"


async def test_callback_select_rejects_unknown_option(wired):
    await _seed_typed(wired, AnswerFormat.SELECT, options=["red", "blue"])
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": "green"}'))
    assert resp.status_code == 400
    state = await wired.store.get_state(wired.fake, "t1")
    assert state is not None
    assert state.status == "pending"  # caller stays blocked


async def test_callback_typed_wrong_type_400(wired):
    await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"answer": 7}'))
    assert resp.status_code == 400
    # A format-validation rejection is re-answerable in place: the door signals it
    # with ``retry_in_place`` so a correlated channel keeps the ask live.
    assert _json(resp) == {"error": "answer must be a string", "retry_in_place": True}


async def test_callback_typed_missing_answer_key_400(wired):
    await _seed_typed(wired, AnswerFormat.TEXT)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"text": "yes"}'))
    assert resp.status_code == 400
    assert _json(resp) == {"error": "body must contain 'answer'"}


async def test_callback_confirm_empty_body_records_true(wired):
    # The GET-confirm page's form POSTs an empty body: an affirmative tap.
    iid = await _seed_typed(wired, AnswerFormat.CONFIRM)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b""))
    assert resp.status_code == 200
    state = await wired.store.get_state(wired.fake, iid)
    assert state is not None
    assert state.response is not None
    assert state.response.answer is True


@pytest.mark.parametrize(("fmt", "options"), [(AnswerFormat.TEXT, None), (AnswerFormat.SELECT, ["red", "blue"])])
async def test_callback_non_confirm_empty_body_400(wired, fmt, options):
    await _seed_typed(wired, fmt, options=options)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b""))
    assert resp.status_code == 400
    assert _json(resp) == {"error": "body must contain 'answer'"}


@pytest.mark.parametrize(("fmt", "options"), [(AnswerFormat.TEXT, None), (AnswerFormat.SELECT, ["red", "blue"])])
async def test_get_typed_value_format_serves_reply_page(wired, fmt, options):
    # These formats reject the confirm form's empty-body POST, so GET serves
    # the byte-constant awaiting-reply page — no form, no action that can fail.
    await _seed_typed(wired, fmt, options=options)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body)
    assert body == _REPLY_PAGE.encode()
    assert b"<form" not in body
    assert resp.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    state = await wired.store.get_state(wired.fake, "t1")
    assert state is not None
    assert state.status == "pending"  # GET never mutates


async def test_get_channel_confirm_serves_confirm_page(wired):
    # confirm keeps the tappable page: its empty-body POST records True.
    await _seed_typed(wired, AnswerFormat.CONFIRM)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    assert bytes(resp.body) == _CONFIRM_PAGE.encode()


async def test_add_data_channel_is_conditional(wired):
    # The add frame carries ``channel`` when the request has one and omits the
    # key otherwise — the exact ``server_verified`` conditional-key shape.
    with_channel = _typed_request(wired.store, AnswerFormat.TEXT)
    assert _add_data(with_channel)["channel"] == "chan"
    without_channel = _external_request(wired.store)
    assert "channel" not in _add_data(without_channel)


def test_add_data_attribution_is_conditional(wired):
    # recipient/origin/audience each ride the add frame when set and are ABSENT
    # (no key, not null) when None — the additive channel/media idiom.
    base = _plain_request(wired.store, AnswerFormat.TEXT)
    bare = _add_data(base)
    assert "recipient" not in bare
    assert "origin" not in bare
    assert "audience" not in bare

    attributed = base.model_copy(update={"recipient": "+15550001111", "origin": "run-xyz", "audience": "alice"})
    data = _add_data(attributed)
    # A delivery address rides as-is, unmasked, on this authed operator feed.
    assert data["recipient"] == "+15550001111"
    assert data["origin"] == "run-xyz"
    assert data["audience"] == "alice"


def test_add_data_attribution_leaves_verifier_strip_intact(wired):
    # Attribution keys are additive: a verifier-bound external question still
    # strips its verifier and flags server_verified alongside the audience key.
    req = _external_request(wired.store, "iva", "gva", verifier=_BINDING).model_copy(update={"audience": "alice"})
    data = _add_data(req)
    assert "verifier" not in (data["format_payload"] or {})
    assert data["server_verified"] is True
    assert data["audience"] == "alice"


# -- the store-unconfigured OFF gate ------------------------------------
# With no interactions Redis the surface is honestly OFF. The gate reads the presence
# env fresh, so delenv-ing BOTH the feature var and the shared default (overriding the
# autouse setenv) forces it.


async def test_callback_get_off_when_store_unconfigured_plain_404(wired):
    # The unauthenticated callback door answers its OWN uniform 404 when the store is
    # OFF — byte-identical to an unknown ticket, never a discriminable 501 oracle.
    wired.monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    wired.monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 404
    assert bytes(resp.body) == b"Not Found"


async def test_callback_post_off_when_store_unconfigured_json_404(wired):
    # The POST callback door mirrors the unknown-ticket 404 JSON body when the store
    # is OFF — the outsider learns nothing about the store's absence.
    wired.monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    wired.monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=b"{}"))
    assert resp.status_code == 404
    assert _json(resp) == {"error": "not found"}


async def test_callback_post_size_cap_identical_off_and_configured(wired):
    # The size cap must not oracle the store's presence: an oversized POST answers a
    # byte-identical 413 whether the store is OFF or configured-but-ticket-unknown, and a
    # normal POST answers the byte-identical uniform 404 in both states. (Configured uses
    # the wired fake Redis; the ticket "NOPE" resolves to nothing → the same 404 an OFF
    # door serves, so the size guard is the ONLY reason 413 differs from 404.)
    wired.settings.callback_max_body_bytes = 10
    oversized = b"x" * 100

    # Configured-but-ticket-unknown (autouse fixture leaves the store on).
    big_on = await router.callback(make_request("POST", path_params={"ticket": "NOPE"}, body=oversized))
    small_on = await router.callback(make_request("POST", path_params={"ticket": "NOPE"}, body=b"{}"))

    # OFF: same door, no store.
    _off(wired)
    big_off = await router.callback(make_request("POST", path_params={"ticket": "NOPE"}, body=oversized))
    small_off = await router.callback(make_request("POST", path_params={"ticket": "NOPE"}, body=b"{}"))

    assert big_on.status_code == big_off.status_code == 413
    assert bytes(big_on.body) == bytes(big_off.body)
    assert small_on.status_code == small_off.status_code == 404
    assert bytes(small_on.body) == bytes(small_off.body)


async def test_callback_post_oversized_query_413_when_off(wired):
    # The query-string leg of the cap fires in the OFF gate too — an oversized query
    # answers 413, not the uniform 404.
    wired.settings.callback_max_body_bytes = 10
    _off(wired)
    resp = await router.callback(make_request("POST", path_params={"ticket": "NOPE"}, query="a=" + "z" * 100))
    assert resp.status_code == 413
