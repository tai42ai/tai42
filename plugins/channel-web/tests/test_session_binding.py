"""A session is a capability on ONE web route: every door refuses a session minted
on another route exactly as it refuses a missing one, so no cookie can be probed for
the routes it serves."""

from __future__ import annotations

import pytest
from starlette.responses import StreamingResponse

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.store.registrations import resolve_session

from .conftest import (
    _ANSWER,
    _CHAT,
    _MESSAGES,
    _ROTATE,
    _ROTATE_URL,
    _STREAM,
    _SUBRESOURCE,
    IDENTITY,
    OTHER_IDENTITY,
    SECURE_COOKIE,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeHttpx,
    FakeRedis,
    _answer_request,
    _body,
    _chat_request,
    _close_body,
    _handler,
    _refusal,
    _rotate_request,
    _seed_question,
    _set_cookie,
    _stream_request,
    build_request,
)

pytestmark = pytest.mark.usefixtures("web_env")


async def test_the_chat_page_replaces_a_session_minted_on_another_route(
    web_env, stub_app, registered_session: FakeRedis, public_build
):
    # A cookie is one per browser and per origin, so opening a second route's chat
    # page must mint that route's own session rather than carry the first one over —
    # and it is replaced exactly as an unregistered cookie is.
    resp = await _handler(stub_app, _CHAT)(_chat_request(identity=OTHER_IDENTITY, token=SESSION_TOKEN))

    minted = _set_cookie(resp)[SECURE_COOKIE].value
    assert minted != SESSION_TOKEN
    registration = await resolve_session(minted)
    assert registration is not None
    assert registration.identity == OTHER_IDENTITY
    assert registration.visitor_id != VISITOR_ID


async def test_the_chat_page_of_a_foreign_session_still_needs_a_navigation_to_mint(
    web_env, stub_app, registered_session: FakeRedis, public_build
):
    # A foreign session is no session here, so the mint guard applies to it in full:
    # a cross-site subresource must not swap a live visitor's cookie for another
    # route's fresh one.
    resp = await _handler(stub_app, _CHAT)(
        _chat_request(identity=OTHER_IDENTITY, token=SESSION_TOKEN, extra_headers=_SUBRESOURCE)
    )

    assert resp.status_code == 403
    _refusal(resp, "not_a_navigation")
    assert resp.headers.getlist("set-cookie") == []


async def test_messages_on_the_session_own_route_are_bridged(web_env, stub_app, registered_session: FakeRedis):
    body = {"identity": IDENTITY, "text": "hello"}
    resp = await _handler(stub_app, _MESSAGES)(build_request(json_body=body, token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["our_identity"] == IDENTITY


async def test_messages_on_another_route_are_refused_exactly_like_no_session(
    web_env, stub_app, registered_session: FakeRedis, fake_redis: FakeRedis
):
    # THE bind: a session minted on one web route buys nothing on another. The
    # refusal must be the one a caller with no cookie at all gets — same status, same
    # code, same message — or the door would confirm that the cookie is a live
    # session for some other route.
    handler = _handler(stub_app, _MESSAGES)
    foreign = await handler(build_request(json_body={"identity": OTHER_IDENTITY, "text": "x"}, token=SESSION_TOKEN))
    absent = await handler(build_request(json_body={"identity": OTHER_IDENTITY, "text": "x"}))

    assert foreign.status_code == absent.status_code == 401
    assert _body(foreign) == _body(absent)
    assert foreign.headers.items() == absent.headers.items()
    # And it buys no turn: the bridge is never reached.
    assert stub_app.conversations.accept_calls == []


async def test_the_stream_of_another_route_is_refused_exactly_like_no_session(
    web_env, stub_app, registered_session: FakeRedis
):
    handler = _handler(stub_app, _STREAM)
    foreign = await handler(_stream_request(query=f"identity={OTHER_IDENTITY}", token=SESSION_TOKEN))
    absent = await handler(_stream_request(query=f"identity={OTHER_IDENTITY}"))

    assert foreign.status_code == absent.status_code == 401
    assert _body(foreign) == _body(absent)
    assert foreign.headers.items() == absent.headers.items()


async def test_the_stream_of_the_session_own_route_is_served(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert isinstance(resp, StreamingResponse)
    await _close_body(resp)


async def test_a_question_asked_on_another_route_is_not_this_session_s_to_answer(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # The answer door reads no identity from the request, so the bind is checked
    # against the record: same address, other route — still not this caller's
    # conversation, and reported as not found like any foreign question.
    await _seed_question(identity=OTHER_IDENTITY)

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))

    assert resp.status_code == 404
    assert "question not found" in _body(resp)["error"]
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in registered_session.store


async def test_a_rotation_binds_the_fresh_session_to_the_route_it_names(
    web_env, stub_app, registered_session: FakeRedis
):
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(OTHER_IDENTITY, token=SESSION_TOKEN))

    assert resp.status_code == 200
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.identity == OTHER_IDENTITY


@pytest.mark.parametrize("body", [{}, {"identity": "  "}, {"identity": "site:alpha"}])
async def test_a_rotation_without_a_usable_route_is_422(web_env, stub_app, registered_session: FakeRedis, body: dict):
    # The fresh session must belong to a route; a rotation that names none would mint
    # a session no door could ever serve.
    resp = await _handler(stub_app, _ROTATE)(build_request(path=_ROTATE_URL, json_body=body, token=SESSION_TOKEN))

    assert resp.status_code == 422
    assert "identity" in _body(resp)["error"]
    # The session it was asked to replace is untouched.
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.visitor_id == VISITOR_ID


async def test_two_routes_in_one_browser_never_share_a_conversation(
    web_env, stub_app, fake_redis: FakeRedis, public_build
):
    # End to end, the way a visitor meets it: open route A, open route B, and the
    # cookie B leaves behind addresses B's conversation only.
    chat = _handler(stub_app, _CHAT)
    token_a = _set_cookie(await chat(_chat_request()))[SECURE_COOKIE].value
    token_b = _set_cookie(await chat(_chat_request(identity=OTHER_IDENTITY, token=token_a)))[SECURE_COOKIE].value

    messages = _handler(stub_app, _MESSAGES)
    to_b = await messages(build_request(json_body={"identity": OTHER_IDENTITY, "text": "x"}, token=token_b))
    to_a = await messages(build_request(json_body={"identity": IDENTITY, "text": "x"}, token=token_b))

    assert to_b.status_code == 200
    assert to_a.status_code == 401
    assert [call["our_identity"] for call in stub_app.conversations.accept_calls] == [OTHER_IDENTITY]
