"""The inbound-message doors — message bridging (locale, address, retry-key dedup,
the shed-reply write-order gate) and ask-less form submission (rendered text, the
no-trust values contract, and the uniform not-found)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest
from starlette.responses import StreamingResponse
from tai42_contract.conversations import BlankInboundTextError, inbound_media_placeholder
from tai42_contract.interactions import MediaAlreadyBoundError, MediaKind, MediaNotFoundError

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.channel import WebChannel

from .conftest import (
    _FOREIGN_ORIGIN,
    _MESSAGES,
    _STREAM,
    _TRANSCRIPT_KEY,
    CLIENT_HOST,
    IDENTITY,
    ORIGIN,
    OTHER_IDENTITY,
    SECURE_COOKIE,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeRedis,
    _body,
    _handler,
    _seed_old_shape_record,
    _stream_request,
    build_request,
    make_ingested_media,
    make_notification,
    register,
)

pytestmark = pytest.mark.usefixtures("web_env")


# -- POST /messages -------------------------------------------------------------


async def test_messages_map_the_top_accept_language_to_the_turn_locale(
    web_env, stub_app, registered_session: FakeRedis
):
    stub_app.conversations.accept_result = "turn-42"
    handler = _handler(stub_app, _MESSAGES)

    resp = await handler(
        build_request(
            json_body={"identity": IDENTITY, "text": "hi"},
            token=SESSION_TOKEN,
            extra_headers=[(b"accept-language", b"he-IL,he;q=0.9,en;q=0.8")],
        )
    )

    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["locale"] == "he-IL"


async def test_messages_without_accept_language_carry_no_locale(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_result = "turn-42"
    handler = _handler(stub_app, _MESSAGES)

    resp = await handler(build_request(json_body={"identity": IDENTITY, "text": "hi"}, token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["locale"] is None


async def test_messages_bridges_and_appends_inbound(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_result = "turn-42"
    handler = _handler(stub_app, _MESSAGES)

    resp = await handler(build_request(json_body={"identity": IDENTITY, "text": "ship it"}, token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"message_id": "turn-42"}}
    assert resp.headers["x-content-type-options"] == "nosniff"
    call = stub_app.conversations.accept_calls[0]
    assert call["channel"] == "web"
    assert call["our_identity"] == IDENTITY
    # The address is the REGISTERED visitor id — never the cookie's secret, and never
    # a body value.
    assert call["client_address"] == VISITOR_ID
    assert call["text"] == "ship it"
    assert len(call["provider_message_id"]) == 32
    # The inbound entry is appended only after accept, reusing the turn id.
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload == {"id": "turn-42", "direction": "in", "text": "ship it", "ts": payload["ts"]}


async def test_the_turn_cap_keys_on_the_network_bucket_not_the_resettable_visitor_id(
    web_env, stub_app, fake_redis: FakeRedis
):
    # The rotate-reset attack: the platform mints the visitor id on an unauthenticated
    # door and the visitor rotates it at will, so a cap keyed on the visitor id would
    # reset every message. Two sessions minted from the SAME network client must share
    # ONE accountable cap key — the request's network bucket — so the cap bounds them.
    _SECOND_TOKEN = "MZ3n-visitor_session-token-9876543210"
    _SECOND_VISITOR = "vis-9876543210cd"
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    register(fake_redis, _SECOND_TOKEN, _SECOND_VISITOR, IDENTITY)
    handler = _handler(stub_app, _MESSAGES)

    await handler(build_request(json_body={"identity": IDENTITY, "text": "one"}, token=SESSION_TOKEN))
    await handler(build_request(json_body={"identity": IDENTITY, "text": "two"}, token=_SECOND_TOKEN))

    first, second = stub_app.conversations.accept_calls
    # Two distinct conversations (two minted visitor ids)...
    assert first["client_address"] == VISITOR_ID
    assert second["client_address"] == _SECOND_VISITOR
    assert first["client_address"] != second["client_address"]
    # ...but ONE accountable cap key: the network bucket, never the resettable id.
    assert first["cap_key"] == second["cap_key"] == CLIENT_HOST
    assert first["cap_key"] != first["client_address"]


async def test_the_reply_message_id_is_the_id_on_the_visitor_own_sse_frame(
    web_env, stub_app, registered_session: FakeRedis
):
    # The page draws a message optimistically on POST and retires that bubble when the
    # same id arrives on the stream. Anything short of EXACT equality between the
    # reply's ``message_id`` and the ``chat.message`` frame's ``id`` leaves the
    # optimistic bubble standing next to the replayed one — two bubbles for one
    # message.
    stub_app.conversations.accept_result = "turn-42"
    sent = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "ship it"}, token=SESSION_TOKEN)
    )
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))

    assert isinstance(resp, StreamingResponse)
    frames = [str(frame) async for frame in resp.body_iterator]
    event, _, payload = frames[0].partition("\ndata: ")
    assert event == "event: chat.message"
    assert json.loads(payload)["id"] == _body(sent)["data"]["message_id"] == "turn-42"


async def test_messages_canonicalises_the_identity(web_env, stub_app, registered_session: FakeRedis):
    # The bridge trims; an untrimmed identity here would key a transcript the
    # bridge's own writes never reach.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": f"  {IDENTITY} ", "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["our_identity"] == IDENTITY
    assert _TRANSCRIPT_KEY in registered_session.streams


async def test_messages_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(build_request(json_body={"identity": IDENTITY, "text": "x"}))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_messages_with_an_unregistered_token_is_401(web_env, stub_app, fake_redis: FakeRedis):
    # The invented-cookie bypass: a shape-valid token nobody registered opens no
    # conversation, so it cannot mint a fresh address past the bridge's turn caps.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"
    assert stub_app.conversations.accept_calls == []


async def test_messages_with_a_malformed_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, cookie=f"{SECURE_COOKIE}=a:b")
    )
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_messages_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN, extra_headers=_FOREIGN_ORIGIN)
    )
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"


async def test_messages_same_origin_header_is_accepted(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "x"},
            token=SESSION_TOKEN,
            extra_headers=[(b"origin", ORIGIN.encode())],
        )
    )
    assert resp.status_code == 200


async def test_messages_unconfigured_store_is_501(no_web_env, stub_app):
    # A turn must not start when its reply can never be shown.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 501
    assert _body(resp)["code"] == "web_transcript_store_off"
    assert stub_app.conversations.accept_calls == []


async def test_messages_invalid_json(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(build_request(raw_body=b"{not json", token=SESSION_TOKEN))
    assert resp.status_code == 400


async def test_messages_oversized_body_is_413(web_env, stub_app, registered_session: FakeRedis, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_BODY_BYTES", "64")
    reset_all_settings()
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x" * 500}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 413
    assert stub_app.conversations.accept_calls == []


async def test_messages_over_long_text_is_422(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x" * 8001}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422
    assert stub_app.conversations.accept_calls == []


@pytest.mark.parametrize("identity", ["  ", "site:alpha", "a" * 257])
async def test_messages_rejects_an_unusable_identity(web_env, stub_app, registered_session: FakeRedis, identity: str):
    # A ``:`` would split the composite recipient and the transcript key.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": identity, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422


async def test_messages_blank_text_maps_to_400(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_error = BlankInboundTextError("blank")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "   "}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 400
    # A refused message never pollutes the transcript.
    assert not registered_session.streams


async def test_messages_unrouted_maps_to_404(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_error = LookupError("no web route named 'site-alpha'")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 404
    assert "no web route" in _body(resp)["error"]


async def test_messages_thread_overflow_maps_to_503(web_env, stub_app, registered_session: FakeRedis):
    class ThreadQueueOverflowError(Exception):
        pass

    stub_app.conversations.accept_error = ThreadQueueOverflowError("thread queue full")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 503


async def test_messages_mints_a_fresh_dedup_id_without_a_retry_key(web_env, stub_app, registered_session: FakeRedis):
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "x"}
    await handler(build_request(json_body=body, token=SESSION_TOKEN))
    await handler(build_request(json_body=body, token=SESSION_TOKEN))

    minted = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert len(set(minted)) == 2
    assert all(len(value) == 32 for value in minted)


async def test_messages_derive_the_dedup_id_from_a_retry_key_and_the_visitor(
    web_env, stub_app, registered_session: FakeRedis
):
    # A POST whose reply never reached the browser is re-sent with the SAME key; the
    # bridge dedups on (channel, provider_message_id) and returns the first turn, so
    # Retry cannot deliver the message twice.
    stub_app.conversations.accept_result = "turn-first"
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "ship it", "client_message_id": "abc-123_XY"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    retry = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    assert _body(first) == _body(retry) == {"data": {"message_id": "turn-first"}}
    sent = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert sent[0] == sent[1]
    # Scoped to the whole conversation — the web route AND the caller's own address,
    # neither of which is the caller's to choose. One visitor can therefore never
    # reach into another's dedup space, and one conversation never into another's.
    assert sent[0] == hashlib.sha256(f"{IDENTITY}:{VISITOR_ID}:abc-123_XY".encode()).hexdigest()


async def test_the_same_retry_key_from_another_visitor_is_a_different_dedup_id(
    web_env, stub_app, fake_redis: FakeRedis
):
    other_token = "OTHER-visitor_session-token-0123456789"
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    register(fake_redis, other_token, "vis-other-000", IDENTITY)
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "x", "client_message_id": "shared-key-1"}

    await handler(build_request(json_body=body, token=SESSION_TOKEN))
    await handler(build_request(json_body=body, token=other_token))

    sent = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert sent[0] != sent[1]


async def test_the_same_retry_key_on_another_web_route_is_a_different_dedup_id(
    web_env, stub_app, registered_session: FakeRedis
):
    # A conversation is ``(identity, address)`` everywhere, and the bridge dedups
    # identity-blind. Keyed on the retry key alone, the second route's send would
    # resolve to the FIRST route's turn: no turn would run, and the new text would be
    # appended to the first route's transcript under the old turn's id. The visitor
    # reaches the second route with its OWN session — a session serves one route.
    other_token = "BETA-visitor_session-token-01234567890"
    other_visitor = "vis-beta-00000"
    register(registered_session, other_token, other_visitor, OTHER_IDENTITY)
    stub_app.conversations.accept_result = "turn-alpha"
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "for alpha", "client_message_id": "shared-key-1"}
    other = {"identity": OTHER_IDENTITY, "text": "for beta", "client_message_id": "shared-key-1"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    stub_app.conversations.accept_result = "turn-beta"
    second = await handler(build_request(json_body=other, token=other_token))

    sent = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert sent[0] != sent[1]
    # Both turns ran, and each text landed on its own route's transcript.
    assert _body(first) == {"data": {"message_id": "turn-alpha"}}
    assert _body(second) == {"data": {"message_id": "turn-beta"}}
    assert [json.loads(e[1]["data"])["text"] for e in registered_session.streams[_TRANSCRIPT_KEY]] == ["for alpha"]
    beta_key = f"channel:web:transcript:{OTHER_IDENTITY}:{other_visitor}"
    assert [json.loads(e[1]["data"])["text"] for e in registered_session.streams[beta_key]] == ["for beta"]


class _IdempotentConversations:
    """The bridge's own dedup contract as ``accept`` states it: idempotent on
    ``(channel, provider_message_id)`` — a redelivery returns the first attempt's
    ``message_id`` and starts no second turn."""

    def __init__(self) -> None:
        self.accept_calls: list[dict[str, Any]] = []
        self.turns: dict[tuple[str, str], str] = {}

    async def accept(self, **kwargs: Any) -> str:
        self.accept_calls.append(kwargs)
        key = (kwargs["channel"], kwargs["provider_message_id"])
        owner = self.turns.get(key)
        if owner is not None:
            return owner
        return self.turns.setdefault(key, f"turn-{len(self.turns) + 1}")


async def test_a_retry_of_the_same_key_replaces_its_own_frame_in_place(
    web_env, stub_app, registered_session: FakeRedis
):
    # The other trigger of a dedup collision: the SAME conversation re-POSTing the
    # key. That is what a retry is — the bridge returns the first attempt's turn, and
    # the frame is re-appended under that same id, which the page replaces in place.
    # So one turn runs and the replay still shows one message.
    stub_app.conversations = _IdempotentConversations()
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "ship it", "client_message_id": "abc-123_XY"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    retry = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    # ONE turn behind two POSTs, and the retry answers with the first attempt's id.
    assert stub_app.conversations.turns == {
        ("web", stub_app.conversations.accept_calls[0]["provider_message_id"]): "turn-1"
    }
    assert _body(first) == _body(retry) == {"data": {"message_id": "turn-1"}}
    entries = [json.loads(entry[1]["data"]) for entry in registered_session.streams[_TRANSCRIPT_KEY]]
    assert [entry["id"] for entry in entries] == ["turn-1", "turn-1"]
    # And each frame carries the visitor's own key, so the page can retire the
    # optimistic bubble it drew for the attempt whose response never arrived.
    assert [entry["client_message_id"] for entry in entries] == ["abc-123_XY", "abc-123_XY"]


async def test_two_messages_without_a_retry_key_are_two_turns(web_env, stub_app, registered_session: FakeRedis):
    # The other side of the same contract: with no key every POST mints a fresh dedup
    # id, so a visitor who really does send the same text twice gets two turns and two
    # bubbles rather than one silently swallowed message.
    stub_app.conversations = _IdempotentConversations()
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "ship it"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    second = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    assert len(stub_app.conversations.turns) == 2
    assert _body(first) == {"data": {"message_id": "turn-1"}}
    assert _body(second) == {"data": {"message_id": "turn-2"}}
    entries = [json.loads(entry[1]["data"]) for entry in registered_session.streams[_TRANSCRIPT_KEY]]
    assert [entry["id"] for entry in entries] == ["turn-1", "turn-2"]


async def test_a_message_without_a_retry_key_echoes_none(web_env, stub_app, registered_session: FakeRedis):
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert "client_message_id" not in payload


@pytest.mark.parametrize("key", ["short", "a" * 65, "has spaces", "bad/char!!", 17])
async def test_messages_reject_a_malformed_retry_key(web_env, stub_app, registered_session: FakeRedis, key: object):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x", "client_message_id": key}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422
    assert "client_message_id" in _body(resp)["error"]
    assert stub_app.conversations.accept_calls == []


async def test_a_shed_reply_can_never_precede_the_message_that_caused_it(
    web_env, stub_app, registered_session: FakeRedis
):
    # A rate-shed message's slow-down reply is spawned INSIDE accept, so without the
    # conversation's write-order gate it XADDs before the visitor's own frame and the
    # page replays the agent answering a message that is not there yet.
    spawned: list[asyncio.Task[list[str]]] = []

    class _SheddingConversations:
        def __init__(self) -> None:
            self.accept_calls: list[dict[str, Any]] = []

        async def accept(self, **kwargs: Any) -> str:
            task = asyncio.create_task(WebChannel().notify(make_notification(message="Slow down.")))
            spawned.append(task)
            # Let the spawned reply run until it needs the transcript.
            await asyncio.sleep(0)
            return "turn-shed"

    stub_app.conversations = _SheddingConversations()
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "ship it"}, token=SESSION_TOKEN)
    )
    await asyncio.gather(*spawned)

    assert resp.status_code == 200
    texts = [json.loads(entry[1]["data"])["text"] for entry in registered_session.streams[_TRANSCRIPT_KEY]]
    assert texts == ["ship it", "Slow down."]


async def test_messages_deeply_nested_body_is_a_400_not_a_recursion_error(
    web_env, stub_app, registered_session: FakeRedis
):
    # ``json.loads`` raises RecursionError (not ValueError) past the interpreter's
    # recursion limit, and a ~40KiB body nests far deeper than that while staying
    # under the body cap — an unhandled one is an opaque 500 on a public door. The
    # shared body parse refuses it as invalid JSON instead, the same clean refusal
    # the contract's iterative depth walk gives deep nesting.
    nested = b'{"identity": "x", "text": ' + b"[" * 20000 + b"]" * 20000 + b"}"
    resp = await _handler(stub_app, _MESSAGES)(build_request(raw_body=nested, token=SESSION_TOKEN))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid JSON body"


async def test_messages_unexpected_error_propagates(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_error = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await _handler(stub_app, _MESSAGES)(
            build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
        )


async def test_message_door_takes_the_no_session_refusal_over_an_old_shape_record(
    web_env, stub_app, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    _seed_old_shape_record(fake_redis)
    with caplog.at_level("WARNING"):
        resp = await _handler(stub_app, _MESSAGES)(
            build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
        )
    # The shared session helper catches the decode error and the door takes its normal
    # no-session 401 — never a 500.
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"
    assert stub_app.conversations.accept_calls == []


async def test_messages_pass_captured_params_to_accept(web_env, stub_app, fake_redis: FakeRedis):
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "spring"})
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] == {"ref": "spring"}


async def test_messages_pass_none_when_no_params(web_env, stub_app, registered_session: FakeRedis):
    # Empty params -> None -> the tool payload is byte-identical to today.
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] is None


async def test_messages_thread_a_tapped_reply_id_as_params_reply_id(web_env, stub_app, registered_session: FakeRedis):
    # A media-card reply chip carrying an authored id sends that id back on the message
    # POST; the door threads it as ``params.reply_id`` so the flow reads WHICH option was
    # chosen, not only its echoed text (the same convention every channel keeps).
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "Item A", "reply_id": "opt-a"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] == {"reply_id": "opt-a"}


async def test_messages_merge_a_reply_id_with_the_session_link_params(web_env, stub_app, fake_redis: FakeRedis):
    # The reply id rides ALONGSIDE the captured link params, never replacing them.
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "spring"})
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "Item A", "reply_id": "opt-a"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] == {"ref": "spring", "reply_id": "opt-a"}


async def test_messages_without_a_reply_id_carry_no_reply_id_param(web_env, stub_app, registered_session: FakeRedis):
    # A typed message threads no reply_id — the payload stays byte-identical to a plain send.
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "typed"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] is None


@pytest.mark.parametrize(
    "reply_id",
    ["", "  ", "a\nb", "a b", "x" * 257],
)
async def test_messages_refuse_a_malformed_reply_id_with_422(
    web_env, stub_app, registered_session: FakeRedis, reply_id: str
):
    # A reply id rides the wire and becomes an entry-param value, so a blank, multi-line,
    # whitespace-bearing, or over-cap token is refused at the body — never bridged.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x", "reply_id": reply_id}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422
    assert stub_app.conversations.accept_calls == []


# -- POST /messages with attachments --------------------------------------------

_ATTACHMENT_A = "A" * 43
_ATTACHMENT_B = "B" * 43


async def test_message_with_no_attachments_binds_nothing_and_accepts_without_them(
    web_env, stub_app, registered_session: FakeRedis
):
    # A text-only send binds nothing and hands ``accept`` no attachments, so its payload
    # matches a plain text send and carries no attachments key.
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "hi"}, token=SESSION_TOKEN)
    )
    assert stub_app.media.bind_calls == []
    assert stub_app.conversations.accept_calls[0]["attachments"] is None


async def test_message_with_one_attachment_binds_then_accepts_and_frames_media(
    web_env, stub_app, registered_session: FakeRedis
):
    # Bind BEFORE accept, with the pmid accept uses; accept receives the typed item and
    # the first-item parity params; the visitor's own frame carries the media.
    stub_app.conversations.accept_result = "turn-42"
    ingested = make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE, mime="image/png", size=1234, sha256="d" * 64)
    stub_app.media.bind_results = [ingested]

    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "look", "attachment_ids": [_ATTACHMENT_A]}, token=SESSION_TOKEN
        )
    )

    assert resp.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    bind = stub_app.media.bind_calls[0]
    assert bind["media_id"] == _ATTACHMENT_A
    assert bind["origin"].channel_id == "web"
    # Ownership is keyed on the registered visitor id, and the media binds to the SAME
    # provider message id accept uses.
    assert bind["origin"].participant_identity == VISITOR_ID
    assert bind["origin"].message_id == call["provider_message_id"]
    assert call["attachments"] == [ingested.item]
    assert call["params"] == {
        "media_kind": "image",
        "media_id": _ATTACHMENT_A,
        "media_mime_type": "image/png",
        "media_sha256": "d" * 64,
        "media_size": "1234",
    }
    assert call["text"] == "look"
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload["media"] == [{"kind": "image", "url": f"/api/interactions/media/{_ATTACHMENT_A}"}]
    assert payload["text"] == "look"


async def test_two_attachments_ride_one_turn_with_the_first_items_parity_params(
    web_env, stub_app, registered_session: FakeRedis
):
    # Multiple attachments ride ONE turn; the typed list carries all, the parity params
    # echo the FIRST only (the media_* vocabulary is single-media).
    first = make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE, mime="image/png", size=11, sha256="1" * 64)
    second = make_ingested_media(
        _ATTACHMENT_B, kind=MediaKind.DOCUMENT, mime="application/pdf", size=22, sha256="2" * 64, filename="report.pdf"
    )
    stub_app.media.bind_results = [first, second]

    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "two", "attachment_ids": [_ATTACHMENT_A, _ATTACHMENT_B]},
            token=SESSION_TOKEN,
        )
    )

    assert resp.status_code == 200
    assert [call["media_id"] for call in stub_app.media.bind_calls] == [_ATTACHMENT_A, _ATTACHMENT_B]
    call = stub_app.conversations.accept_calls[0]
    assert call["attachments"] == [first.item, second.item]
    assert call["params"]["media_id"] == _ATTACHMENT_A
    assert call["params"]["media_mime_type"] == "image/png"
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload["media"] == [
        {"kind": "image", "url": f"/api/interactions/media/{_ATTACHMENT_A}"},
        {"kind": "document", "url": f"/api/interactions/media/{_ATTACHMENT_B}", "filename": "report.pdf"},
    ]


async def test_a_blank_text_message_with_an_attachment_accepts_with_a_placeholder_and_a_blank_frame(
    web_env, stub_app, registered_session: FakeRedis
):
    # The turn text handed to accept is the media placeholder (accept refuses blank text);
    # the visitor's own frame keeps the RAW blank text, so the page renders a media-only bubble.
    stub_app.conversations.accept_result = "turn-9"
    stub_app.media.bind_results = [make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE)]

    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "", "attachment_ids": [_ATTACHMENT_A]}, token=SESSION_TOKEN
        )
    )

    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["text"] == inbound_media_placeholder(MediaKind.IMAGE.value)
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload["text"] == ""
    assert payload["media"] == [{"kind": "image", "url": f"/api/interactions/media/{_ATTACHMENT_A}"}]


async def test_an_unbindable_attachment_is_400_media_unbindable(web_env, stub_app, registered_session: FakeRedis):
    # An unknown / expired / not-owned id — one uniform bind error — is a loud 400, and
    # no turn is accepted or appended.
    stub_app.media.bind_error = MediaNotFoundError("gone")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "look", "attachment_ids": [_ATTACHMENT_A]}, token=SESSION_TOKEN
        )
    )
    assert resp.status_code == 400
    assert _body(resp)["code"] == "media_unbindable"
    assert stub_app.conversations.accept_calls == []
    assert not registered_session.streams


async def test_an_already_bound_attachment_is_409_media_already_bound(web_env, stub_app, registered_session: FakeRedis):
    # An id already bound to ANOTHER message is a loud 409 conflict.
    stub_app.media.bind_error = MediaAlreadyBoundError("taken")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "look", "attachment_ids": [_ATTACHMENT_A]}, token=SESSION_TOKEN
        )
    )
    assert resp.status_code == 409
    assert _body(resp)["code"] == "media_already_bound"
    assert stub_app.conversations.accept_calls == []


async def test_over_the_attachment_count_cap_is_422_before_any_bind(
    web_env, stub_app, registered_session: FakeRedis, monkeypatch
):
    # The count cap is the operator's ``max_attachments_per_message`` setting, enforced in
    # the door before any bind — an over-count list never reaches the bind step.
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_ATTACHMENTS_PER_MESSAGE", "2")
    reset_all_settings()
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "x", "attachment_ids": [_ATTACHMENT_A, _ATTACHMENT_B, "C" * 43]},
            token=SESSION_TOKEN,
        )
    )
    assert resp.status_code == 422
    assert "attachment_ids" in _body(resp)["error"]
    assert "2 attachments" in _body(resp)["error"]
    assert stub_app.media.bind_calls == []
    assert stub_app.conversations.accept_calls == []


async def test_at_the_attachment_count_cap_the_message_is_accepted(
    web_env, stub_app, registered_session: FakeRedis, monkeypatch
):
    # The boundary: exactly the cap is accepted — the door binds every id and accepts one
    # turn, so the cap admits its own limit rather than refusing at it.
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_ATTACHMENTS_PER_MESSAGE", "2")
    reset_all_settings()
    stub_app.conversations.accept_result = "turn-cap"
    stub_app.media.bind_results = [
        make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE),
        make_ingested_media(_ATTACHMENT_B, kind=MediaKind.IMAGE),
    ]

    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "two", "attachment_ids": [_ATTACHMENT_A, _ATTACHMENT_B]},
            token=SESSION_TOKEN,
        )
    )

    assert resp.status_code == 200
    assert [call["media_id"] for call in stub_app.media.bind_calls] == [_ATTACHMENT_A, _ATTACHMENT_B]
    assert len(stub_app.conversations.accept_calls) == 1


async def test_media_params_overflowing_the_entry_param_cap_is_422_and_never_accepts(
    web_env, stub_app, fake_redis: FakeRedis
):
    # The first attachment's parity media_* params merge into the session's captured link
    # params and are re-bounded together; a merge over the entry-param count cap is a loud
    # 422 the visitor's send is refused with. The bind precedes the merge, so it has
    # already run — but no turn is accepted and no frame is appended.
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, params={f"k{i}": "v" for i in range(12)})
    stub_app.media.bind_results = [
        make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE, mime="image/png", size=1234, sha256="d" * 64)
    ]

    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "look", "attachment_ids": [_ATTACHMENT_A]}, token=SESSION_TOKEN
        )
    )

    # 12 session params + 5 media_* params (kind, id, mime, sha256, size) = 17, one over
    # the entry-param count cap of 16.
    assert resp.status_code == 422
    assert "over the 16 allowed" in _body(resp)["error"]
    # Binding precedes the merge, so the bind ran; the overflow refuses the message, never
    # the bind (there is no unbind).
    assert stub_app.media.bind_calls[0]["media_id"] == _ATTACHMENT_A
    assert stub_app.conversations.accept_calls == []
    assert not fake_redis.streams


@pytest.mark.parametrize("bad", ["", "short", "A" * 42, "A" * 44, "has/slash" + "A" * 34])
async def test_a_malformed_attachment_id_is_422(web_env, stub_app, registered_session: FakeRedis, bad: str):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x", "attachment_ids": [bad]}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422
    assert stub_app.media.bind_calls == []


async def test_a_repeated_attachment_id_is_422(web_env, stub_app, registered_session: FakeRedis):
    # A repeated id would bind and deliver one served object twice; the wire refuses it.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "x", "attachment_ids": [_ATTACHMENT_A, _ATTACHMENT_A]},
            token=SESSION_TOKEN,
        )
    )
    assert resp.status_code == 422
    assert stub_app.media.bind_calls == []


async def test_a_retry_of_the_same_key_rebinds_to_the_same_message_and_runs_one_turn(
    web_env, stub_app, registered_session: FakeRedis
):
    # A retry re-derives the same provider message id, so each POST re-binds against the
    # SAME message (the seam's bind is idempotent for it) and accept dedups to one turn.
    stub_app.conversations = _IdempotentConversations()
    stub_app.media.bind_results = [
        make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE),
        make_ingested_media(_ATTACHMENT_A, kind=MediaKind.IMAGE),
    ]
    body = {"identity": IDENTITY, "text": "look", "client_message_id": "abc-123_XY", "attachment_ids": [_ATTACHMENT_A]}
    handler = _handler(stub_app, _MESSAGES)

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    retry = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    assert _body(first) == _body(retry)
    assert len(stub_app.conversations.turns) == 1
    origins = [call["origin"].message_id for call in stub_app.media.bind_calls]
    assert len(origins) == 2
    assert origins[0] == origins[1]


# -- POST /forms/{token} --------------------------------------------------------

_FORMS = "/forms/{token}"
_FORM_TOKEN = "f0e1d2c3b4a5968778695a4b3c2d1e0f"
_FORM_KEY = f"channel:web:form:{_FORM_TOKEN}"
_ROUTE_FORM_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "title": "Name"}, "age": {"type": "number"}},
    "required": ["name"],
}
# The uniform not-found wording: unknown, expired and foreign tokens must all read
# byte-identically, so a probe learns nothing from the refusal.
_FORM_NOT_FOUND_BODY = {"error": "form not found (unknown, expired, or already gone)"}


def _seed_form(
    fake: FakeRedis,
    token: str = _FORM_TOKEN,
    identity: str = IDENTITY,
    address: str = VISITOR_ID,
    schema: dict | None = None,
    message: str = "Fill this in",
    form_tag: str | None = None,
) -> None:
    """Seed one form token record exactly as the notify path writes it."""
    fake.store[f"channel:web:form:{token}"] = json.dumps(
        {
            "identity": identity,
            "address": address,
            "schema": schema or _ROUTE_FORM_SCHEMA,
            "message": message,
            "form_tag": form_tag,
        }
    )


_DEFAULT_VALUES = object()  # sentinel: ``None`` is itself a value the door must refuse


def _form_request(
    token: str = _FORM_TOKEN, values: object = _DEFAULT_VALUES, session: str | None = SESSION_TOKEN, **kwargs
):
    body = {"values": values if values is not _DEFAULT_VALUES else {"name": "Ada", "age": 42}}
    body.update(kwargs.pop("extra_body", {}))
    return build_request(
        path=f"/api/channels/web/forms/{token}",
        json_body=body,
        path_params={"token": token},
        token=session,
        **kwargs,
    )


async def test_forms_bridges_values_with_rendered_text_and_appends_inbound(
    web_env, stub_app, registered_session: FakeRedis
):
    # The one accept: the structured values ride ``form`` verbatim while the text is
    # the server-rendered ``label: value`` transcript form — labels from the STORED
    # schema (server-trusted), values from the caller (untrusted data). The visitor's
    # own chat.message frame reuses the accept-returned id, like the messages door.
    stub_app.conversations.accept_result = "turn-77"
    _seed_form(registered_session)

    resp = await _handler(stub_app, _FORMS)(_form_request())

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"message_id": "turn-77"}}
    call = stub_app.conversations.accept_calls[0]
    assert call["channel"] == "web"
    assert call["our_identity"] == IDENTITY
    assert call["client_address"] == VISITOR_ID
    assert call["cap_key"] == CLIENT_HOST
    assert call["text"] == "Name: Ada\nage: 42"
    assert call["form"] == {"name": "Ada", "age": 42}
    assert call["params"] is None
    assert len(call["provider_message_id"]) == 32
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload == {"id": "turn-77", "direction": "in", "text": "Name: Ada\nage: 42", "ts": payload["ts"]}


async def test_forms_round_trip_the_stored_form_tag_to_accept(web_env, stub_app, registered_session: FakeRedis):
    # The caller-set opaque per-send tag the notify path stored on the record rides back to the
    # bridge on submit, beside the form — so the submission carries the correlation.
    stub_app.conversations.accept_result = "turn-77"
    _seed_form(registered_session, form_tag="ref-42")

    resp = await _handler(stub_app, _FORMS)(_form_request())

    assert resp.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form_tag"] == "ref-42"


async def test_forms_ignore_a_client_body_form_tag(web_env, stub_app, registered_session: FakeRedis):
    # The tag rides the SERVER-side record only: a client-supplied form_tag in the body is not a
    # FormSubmissionBody field and is ignored — the record's value (here none) is what reaches accept.
    stub_app.conversations.accept_result = "turn-77"
    _seed_form(registered_session)

    resp = await _handler(stub_app, _FORMS)(_form_request(extra_body={"form_tag": "client-forged"}))

    assert resp.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form_tag"] is None


async def test_forms_schema_violating_values_still_land_in_form(web_env, stub_app, registered_session: FakeRedis):
    # The pinned no-trust contract: this door never validates values against the
    # stored schema — transport bounds only. Participant-shaped data lands in ``form``
    # verbatim (the platform's validate_inbound_form still bounds size/depth at
    # accept), and the consumer treats it as never schema-conformant.
    _seed_form(registered_session)
    values = {"name": 7, "unlisted": [True, None], "age": "not-a-number"}

    resp = await _handler(stub_app, _FORMS)(_form_request(values=values))

    assert resp.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form"] == values
    assert call["text"] == "Name: 7\nage: not-a-number\nunlisted: [true, null]"


async def test_forms_renders_values_the_schema_never_named(web_env, stub_app, registered_session: FakeRedis):
    # A key outside the schema still renders (nothing the consumers see may be
    # silently dropped) — labelled by its own raw key, since the schema names none.
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(values={"extra": "x"}))
    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["text"] == "extra: x"


async def test_forms_foreign_session_is_the_uniform_404(web_env, stub_app, fake_redis: FakeRedis):
    # The record exists but belongs to another conversation: the caller is told
    # exactly what an unknown token is told — never "exists, but not yours".
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    _seed_form(fake_redis, identity=OTHER_IDENTITY)

    resp = await _handler(stub_app, _FORMS)(_form_request())

    assert resp.status_code == 404
    assert _body(resp) == _FORM_NOT_FOUND_BODY
    assert stub_app.conversations.accept_calls == []
    # Read, never claimed: the record still stands for its own conversation.
    assert _FORM_KEY in fake_redis.store


async def test_forms_foreign_address_is_the_uniform_404(web_env, stub_app, fake_redis: FakeRedis):
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    _seed_form(fake_redis, address="vis-someoneelse00")
    resp = await _handler(stub_app, _FORMS)(_form_request())
    assert resp.status_code == 404
    assert _body(resp) == _FORM_NOT_FOUND_BODY


async def test_forms_unknown_or_expired_token_is_the_uniform_404(web_env, stub_app, registered_session: FakeRedis):
    # An expired record and a token that never existed are indistinguishable here —
    # Redis expiry deletes the key — and both must read as the foreign refusal does.
    resp = await _handler(stub_app, _FORMS)(_form_request(token="0" * 32))
    assert resp.status_code == 404
    assert _body(resp) == _FORM_NOT_FOUND_BODY
    assert stub_app.conversations.accept_calls == []


async def test_forms_resubmission_is_its_own_participant_message(web_env, stub_app, registered_session: FakeRedis):
    # The record is READ, never claimed (the chips precedent): a second submission is
    # a second participant message through a second accept, and the card stays answerable
    # until its record ages out with the transcript.
    _seed_form(registered_session)
    first = await _handler(stub_app, _FORMS)(_form_request(values={"name": "Ada"}))
    second = await _handler(stub_app, _FORMS)(_form_request(values={"name": "Grace"}))

    assert first.status_code == second.status_code == 200
    assert [call["form"] for call in stub_app.conversations.accept_calls] == [{"name": "Ada"}, {"name": "Grace"}]
    assert len(registered_session.streams[_TRANSCRIPT_KEY]) == 2
    assert _FORM_KEY in registered_session.store


async def test_forms_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    _seed_form(fake_redis)
    resp = await _handler(stub_app, _FORMS)(_form_request(session=None))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_forms_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"


async def test_forms_unconfigured_store_is_501(no_web_env, stub_app):
    resp = await _handler(stub_app, _FORMS)(_form_request())
    assert resp.status_code == 501
    assert _body(resp)["code"] == "web_transcript_store_off"


async def test_forms_oversized_values_object_is_422(web_env, stub_app, registered_session: FakeRedis):
    # The SAME transport bound the answer door's form branch applies: serialized
    # UTF-8 size, well under the body cap so the refusal is a precise 422.
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(values={"blob": "x" * (32 * 1024)}))
    assert resp.status_code == 422
    assert "32768 bytes" in _body(resp)["error"]
    assert stub_app.conversations.accept_calls == []


async def test_forms_non_finite_number_is_422(web_env, stub_app, registered_session: FakeRedis):
    # ``json.loads`` accepts Infinity/NaN; forwarding one would persist an
    # unparseable token into the transcript — refused by the shared bound.
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(values={"x": float("inf")}))
    assert resp.status_code == 422
    assert "finite" in _body(resp)["error"]


@pytest.mark.parametrize("values", ["text", 7, [1, 2], True, None])
async def test_forms_non_object_values_is_422(web_env, stub_app, registered_session: FakeRedis, values: object):
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(values=values))
    assert resp.status_code == 422


async def test_forms_empty_values_is_422(web_env, stub_app, registered_session: FakeRedis):
    # {} carries nothing to bridge and would render a blank message; a transport
    # shape rule, not schema validation.
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(values={}))
    assert resp.status_code == 422
    assert stub_app.conversations.accept_calls == []


async def test_forms_body_without_values_is_422(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(
        build_request(
            path=f"/api/channels/web/forms/{_FORM_TOKEN}",
            json_body={"answer": {"name": "Ada"}},
            path_params={"token": _FORM_TOKEN},
            token=SESSION_TOKEN,
        )
    )
    assert resp.status_code == 422


async def test_forms_invalid_json_is_400(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(
        build_request(
            path=f"/api/channels/web/forms/{_FORM_TOKEN}",
            raw_body=b"not json",
            path_params={"token": _FORM_TOKEN},
            token=SESSION_TOKEN,
        )
    )
    assert resp.status_code == 400


async def test_forms_derives_the_dedup_id_from_a_retry_key_like_the_messages_door(
    web_env, stub_app, registered_session: FakeRedis
):
    # Same derivation as the messages door: the key AND the whole conversation, so a
    # re-POST resolves to the first attempt's turn and no caller reaches another
    # conversation's dedup space. The key is echoed onto the visitor's own frame.
    _seed_form(registered_session)
    key = "retry-key-0001"
    expected = hashlib.sha256(f"{IDENTITY}:{VISITOR_ID}:{key}".encode()).hexdigest()

    await _handler(stub_app, _FORMS)(_form_request(extra_body={"client_message_id": key}))
    await _handler(stub_app, _FORMS)(_form_request(extra_body={"client_message_id": key}))

    first, second = stub_app.conversations.accept_calls
    assert first["provider_message_id"] == second["provider_message_id"] == expected
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload["client_message_id"] == key


async def test_forms_rejects_a_malformed_retry_key(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)
    resp = await _handler(stub_app, _FORMS)(_form_request(extra_body={"client_message_id": "no spaces!"}))
    assert resp.status_code == 422


async def test_forms_delivers_the_session_link_params(web_env, stub_app, fake_redis: FakeRedis):
    # The captured entry params ride the turn exactly as a typed message's do.
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, params={"topic": "news"})
    _seed_form(fake_redis)
    await _handler(stub_app, _FORMS)(_form_request())
    assert stub_app.conversations.accept_calls[0]["params"] == {"topic": "news"}


async def test_forms_unrouted_maps_to_404(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)
    stub_app.conversations.accept_error = LookupError("no web route matches")
    resp = await _handler(stub_app, _FORMS)(_form_request())
    assert resp.status_code == 404


async def test_forms_thread_overflow_maps_to_503(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)

    class ThreadQueueOverflowError(Exception): ...

    stub_app.conversations.accept_error = ThreadQueueOverflowError("queue full")
    resp = await _handler(stub_app, _FORMS)(_form_request())
    assert resp.status_code == 503


async def test_forms_no_frame_is_appended_when_accept_refuses(web_env, stub_app, registered_session: FakeRedis):
    _seed_form(registered_session)
    stub_app.conversations.accept_error = LookupError("no web route matches")
    await _handler(stub_app, _FORMS)(_form_request())
    assert _TRANSCRIPT_KEY not in registered_session.streams
