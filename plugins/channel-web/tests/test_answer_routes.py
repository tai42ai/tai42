"""The answer door — the forward-and-append happy path, link-param enrichment, the
transport bounds on the forwarded value, the callback status policy, the restore cap,
and the write-order gate on the answered frame."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from tai42_kit.settings import reset_all_settings

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.routes.answer_routes import AnswerForwardError
from tai42_channel_web.store.transcript import transcript_order

from .conftest import (
    _ANSWER,
    _FOREIGN_ORIGIN,
    _TRANSCRIPT_KEY,
    CALLBACK,
    IDENTITY,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeHttpx,
    FakeRedis,
    _answer_request,
    _body,
    _handler,
    _seed_question,
    register,
    response,
)

pytestmark = pytest.mark.usefixtures("web_env")


async def test_answer_forwards_and_appends_answered(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="staging"))

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"status": "answered"}}
    assert fake_httpx.calls[0] == {"url": CALLBACK, "json": {"answer": "staging"}, "headers": None}
    answered = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert answered["interaction_id"] == "int-1"
    assert answered["answer"] == "staging"


async def test_answer_forwards_the_session_link_params_beside_the_answer(
    web_env, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The reporter's path: a session created with link params (a ``GET /chat?ref=spring&n=3``
    # captured on the registration) answers a pending ask through the ANSWER door. The
    # forwarded body must carry those params BESIDE ``answer`` — the same enrichment a
    # MESSAGE turn from this session delivers — so a flow reading ``InteractionResponse.params``
    # sees them on an answer too, not only on messages.
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "spring", "n": "3"})
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="staging"))

    assert resp.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": "staging", "params": {"ref": "spring", "n": "3"}}


async def test_answer_with_over_limit_session_params_is_a_422_and_keeps_the_record(
    web_env, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An over-count captured param set is re-bounded here (the same validator the message
    # door applies), so a bad set is a clean 422 rather than an opaque callback error — and
    # the refusal happens BEFORE the claim, so nothing is forwarded and the record survives
    # for a corrected retry.
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {f"k{i}": "v" for i in range(17)})
    await _seed_question()

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))

    assert resp.status_code == 422
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in fake_redis.store


async def test_answer_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x", token=None))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_answer_with_an_unregistered_token_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 401


async def test_answer_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x", extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"


async def test_answer_unconfigured_store_is_501(no_web_env, stub_app):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 501


async def test_answer_from_another_conversation_is_404_and_keeps_the_record(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # A question is answerable only from the conversation it was asked in — and the
    # refusal never reveals that it exists, nor consumes it.
    await _seed_question(address="other-visitor-id")
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404
    assert "question not found" in _body(resp)["error"]
    assert "channel:web:question:int-1" in registered_session.store
    assert fake_httpx.calls == []


async def test_answer_invalid_json(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=b"{bad"))
    assert resp.status_code == 400


async def test_answer_oversized_body_is_413(web_env, stub_app, registered_session: FakeRedis, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_BODY_BYTES", "64")
    reset_all_settings()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x" * 500))
    assert resp.status_code == 413


async def test_answer_missing_answer_key(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=json.dumps({"nope": 1}).encode()))
    assert resp.status_code == 400


@pytest.mark.parametrize("answer", [["a", "b"], None])
async def test_answer_rejects_a_non_scalar_non_object_value(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, answer
):
    # A web answer is one scalar (text/confirm/select) or a form object; a list or a
    # bare null is neither, and the record is never claimed for a value the door will
    # not forward.
    await _seed_question()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))
    assert resp.status_code == 422
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_forwards_a_form_object(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    # A form answer is a JSON object; it is forwarded as-is and persisted into the
    # chat.answered frame. The callback door validates it against the stored schema —
    # this door only bounds its shape and size.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))
    answer = {"name": "Ada", "count": 3}

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))

    assert resp.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": answer}
    answered = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert answered["answer"] == answer


async def test_answer_rejects_an_oversized_object(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # An object whose serialized size exceeds the object cap is a precise 422 (it fits
    # under the body cap), never forwarded, and the record is kept.
    await _seed_question()
    answer = {"blob": "x" * (33 * 1024)}
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))
    assert resp.status_code == 422
    assert "serialize to at most" in _body(resp)["error"]
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_rejects_a_non_finite_number_inside_an_object(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # ``json.loads`` accepts ``Infinity`` nested in an object; forwarding it would emit
    # invalid JSON to the callback door AND persist a bad token into the transcript.
    await _seed_question()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=b'{"answer": {"x": Infinity}}'))
    assert resp.status_code == 422
    assert _body(resp)["error"] == "answer object must contain only finite numbers"
    assert fake_httpx.calls == []
    assert _TRANSCRIPT_KEY not in registered_session.streams
    assert "channel:web:question:int-1" in registered_session.store


@pytest.mark.parametrize("raw", [b'{"answer": 1e999}', b'{"answer": Infinity}', b'{"answer": NaN}'])
async def test_answer_rejects_a_non_finite_number(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, raw: bytes
):
    # ``json.loads`` accepts these; forwarding one emits invalid JSON to the callback
    # door AND persists an ``Infinity`` token into the transcript, which then fails the
    # page's own JSON parse on every reconnect for the whole transcript TTL.
    await _seed_question()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=raw))

    assert resp.status_code == 422
    assert _body(resp)["error"] == "answer must be a finite number"
    assert fake_httpx.calls == []
    assert _TRANSCRIPT_KEY not in registered_session.streams
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_rejects_an_over_long_string(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x" * 8001))
    assert resp.status_code == 422
    assert "8000 characters" in _body(resp)["error"]


@pytest.mark.parametrize("answer", ["staging", True, 3, 1.5])
async def test_answer_forwards_every_scalar_shape(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, answer
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))
    assert resp.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": answer}


async def test_answer_unknown_question_404(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404


async def test_answer_claim_lost_to_a_duplicate_is_404(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # The ownership peek passed, then a concurrent duplicate POST claimed the record
    # first: the loser answers 404 rather than forwarding a claimless answer.
    await _seed_question()

    async def _claimed_elsewhere(key: str) -> None:
        return None

    monkeypatch.setattr(registered_session, "getdel", _claimed_elsewhere)
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404
    assert fake_httpx.calls == []


async def test_answer_door_reports_already_answered_without_a_false_frame(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # The callback door's lost-claim reply is an idempotent 200, never a 409. The
    # recorded answer is someone else's, so no chat.answered frame is written — it
    # would show the visitor a value that was never recorded — and the record stays
    # dropped.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "already_answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="mine"))

    assert resp.status_code == 409
    assert _body(resp)["error"] == "that question was already answered"
    assert _TRANSCRIPT_KEY not in registered_session.streams
    assert "channel:web:question:int-1" not in registered_session.store


async def test_answer_door_404_is_terminal_not_restored(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(404, json={"error": "gone"}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404
    assert "expired or withdrawn" in _body(resp)["error"]
    assert "channel:web:question:int-1" not in registered_session.store


async def test_answer_door_400_restores_and_surfaces_error(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(400, json={"error": "answer does not match a text question"}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "answer does not match a text question"
    # Kept so the visitor can re-answer.
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_door_400_non_json_body_is_never_relayed_to_the_visitor(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A non-JSON body did not come from the door: it is a proxy or WAF page that
    # answered instead, and it names hosts and software an anonymous visitor is not
    # entitled to. The operator gets it in the log; the visitor gets this door's own
    # refusal.
    await _seed_question()
    fake_httpx.responses.append(response(400, text="<h1>BigProxy 4.2 blocked internal-app-07.corp</h1>"))
    with caplog.at_level("WARNING"):
        resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))

    assert resp.status_code == 400
    assert _body(resp)["error"] == "the answer was refused"
    assert "internal-app-07" not in json.dumps(_body(resp))
    assert any("internal-app-07" in r.getMessage() for r in caplog.records)


async def test_answer_door_400_json_without_the_error_envelope_is_never_relayed(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(400, json={"detail": "no error key"}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "the answer was refused"


async def test_a_refused_answer_is_only_restored_so_many_times(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, monkeypatch, caplog
):
    # A visitor looping an out-of-enum answer would otherwise forward for as long as
    # the question lives, draining that shared callback bucket for every channel.
    monkeypatch.setenv("CHANNEL_WEB_MAX_ANSWER_RESTORES", "2")
    reset_all_settings()
    await _seed_question()
    handler = _handler(stub_app, _ANSWER)
    for _ in range(3):
        fake_httpx.responses.append(response(400, json={"error": "answer does not match a select question"}))

    for _ in range(2):
        refused = await handler(_answer_request(answer="nope"))
        assert refused.status_code == 400
        assert _body(refused)["error"] == "answer does not match a select question"

    with caplog.at_level("ERROR"):
        spent = await handler(_answer_request(answer="nope"))

    assert spent.status_code == 400
    assert _body(spent)["error"].endswith("; that question can no longer be answered")
    # The record is left dropped — a sane state: the ask resolves by its own timeout.
    assert "channel:web:question:int-1" not in registered_session.store
    assert any("already been refused" in r.getMessage() for r in caplog.records)
    # And the loop stops there: the next attempt never reaches the callback door.
    assert (await handler(_answer_request(answer="nope"))).status_code == 404
    assert len(fake_httpx.calls) == 3


async def test_the_answered_frame_takes_the_write_order_gate(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # Every agent-side append takes the conversation's gate; this frame must take it too,
    # or the reply an answer sets off could XADD ahead of the frame that
    # settles the question the reply is answering.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))
    holder = asyncio.Event()

    async def _hold_the_gate() -> None:
        async with transcript_order(IDENTITY, VISITOR_ID):
            holder.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    held = asyncio.create_task(_hold_the_gate())
    await holder.wait()
    answering = asyncio.ensure_future(_handler(stub_app, _ANSWER)(_answer_request(answer="x")))
    await asyncio.sleep(0)
    # The append cannot land while another writer holds the conversation's gate.
    assert _TRANSCRIPT_KEY not in registered_session.streams

    await held
    assert (await answering).status_code == 200
    assert _TRANSCRIPT_KEY in registered_session.streams


async def test_answer_non_json_2xx_still_appends(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # A 2xx whose body is not the envelope is an accepted answer, not a lost claim.
    await _seed_question()
    fake_httpx.responses.append(response(200, text="ok"))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 200
    assert _TRANSCRIPT_KEY in registered_session.streams


async def test_answer_2xx_envelope_without_status_still_appends(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json=["not", "an", "envelope"]))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 200


async def test_answer_door_other_status_restores_and_raises(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(500, text="upstream boom"))
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_transport_failure_restores_and_raises(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(httpx.ConnectError("no route to host"))
    with pytest.raises(httpx.HTTPError):
        await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert "channel:web:question:int-1" in registered_session.store
