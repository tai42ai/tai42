"""The reaction door — the sibling of the answer door for a reacting form.

It runs the SAME session + cross-origin + question-ownership checks, forwards the event and
the values filled so far SERVER-SIDE to the interaction's ticket react door (the ticket held
in the stored record, never in the browser), returns the bare form update, records nothing
(the question stays open), and surfaces a refused/failed reaction loudly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.routes.reaction_routes import ReactionForwardError

from .conftest import (
    _FOREIGN_ORIGIN,
    CALLBACK,
    OTHER_IDENTITY,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeHttpx,
    FakeRedis,
    _body,
    _handler,
    _seed_question,
    build_request,
    register,
    response,
)

pytestmark = pytest.mark.usefixtures("web_env")

_REACT = "/questions/{interaction_id}/react"
# The ticket react door this door forwards to: the stored callback sink plus its /react
# sibling. The ticket lives only inside CALLBACK (server-side), never in the browser.
_REACT_URL = f"{CALLBACK}/react"
_EVENT = {"kind": "field_changed", "field": "colour"}
_VALUES = {"colour": "red"}


def _react_request(
    interaction_id: str = "int-1",
    event: Any = None,
    values: Any = None,
    raw_body: bytes | None = None,
    token: str | None = SESSION_TOKEN,
    **kwargs: Any,
) -> Request:
    json_body = (
        None
        if raw_body is not None
        else {"event": _EVENT if event is None else event, "values": _VALUES if values is None else values}
    )
    return build_request(
        path=_REACT.format(interaction_id=interaction_id),
        json_body=json_body,
        raw_body=raw_body,
        path_params={"interaction_id": interaction_id},
        token=token,
        **kwargs,
    )


async def test_react_forwards_to_the_ticket_react_door_and_returns_the_update(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # The happy path: the event + values filled so far are forwarded to the interaction's
    # ticket react door, and the validated form update comes back to the visitor as the
    # door's own {"data": <update>} body — the bare update the widget applies.
    await _seed_question()
    update = {"values": {"total": 5}, "options": {"size": [{"value": "m", "label": "Medium"}]}}
    fake_httpx.responses.append(response(200, json={"data": {"update": update}}))

    resp = await _handler(stub_app, _REACT)(_react_request())

    assert resp.status_code == 200
    assert _body(resp) == {"data": update}
    # Forwarded server-side to the ticket react door with the event + partial values; the
    # ticket is only ever in that URL, held server-side, never handed to the browser.
    assert fake_httpx.calls[0] == {"url": _REACT_URL, "json": {"event": _EVENT, "values": _VALUES}, "headers": None}
    # Stateless: the record is read, never claimed — the question stays open.
    assert "channel:web:question:int-1" in registered_session.store


async def test_react_keeps_the_ticket_off_the_browser(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # The response the browser gets names no ticket: the door derives the ticket react URL
    # from the stored callback_url server-side and returns only the form update.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"update": {"values": {"x": 1}}}}))

    resp = await _handler(stub_app, _REACT)(_react_request())

    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    assert "ticket-1" not in body
    assert "callback" not in body


async def test_react_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _REACT)(_react_request(token=None))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_react_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _REACT)(_react_request(extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"


async def test_react_unconfigured_store_is_501(no_web_env, stub_app):
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 501


async def test_react_from_another_conversation_is_404_and_does_not_forward(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # A reaction is reachable only from the conversation the form was asked in; a foreign
    # one is reported as not found and never forwarded, exactly as the answer door refuses.
    await _seed_question(address="other-visitor-id")
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 404
    assert "question not found" in _body(resp)["error"]
    assert fake_httpx.calls == []


async def test_react_unknown_question_is_404(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 404
    assert fake_httpx.calls == []


async def test_react_missing_keys_is_400(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_question()
    resp = await _handler(stub_app, _REACT)(_react_request(raw_body=json.dumps({"event": _EVENT}).encode()))
    assert resp.status_code == 400
    assert "'event' and 'values'" in _body(resp)["error"]
    assert fake_httpx.calls == []


@pytest.mark.parametrize("event", [{"no": "kind"}, "notobject", {"kind": "  "}])
async def test_react_bad_event_shape_is_422(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, event
):
    await _seed_question()
    resp = await _handler(stub_app, _REACT)(_react_request(event=event))
    assert resp.status_code == 422
    assert fake_httpx.calls == []


async def test_react_oversized_values_is_422(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_question()
    resp = await _handler(stub_app, _REACT)(_react_request(values={"blob": "x" * (33 * 1024)}))
    assert resp.status_code == 422
    assert "serialize to at most" in _body(resp)["error"]
    assert fake_httpx.calls == []


@pytest.mark.parametrize(
    ("door_status", "expected"),
    [(400, 400), (409, 409), (413, 413), (502, 502)],
)
async def test_react_relays_the_ticket_door_refusal(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, door_status, expected
):
    # A refused/failed reaction surfaces loudly at the same status the ticket react door
    # gave, carrying that door's own error message (never a stale/silent value).
    await _seed_question()
    fake_httpx.responses.append(response(door_status, json={"error": "nope"}))
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == expected
    assert _body(resp)["error"] == "nope"


async def test_react_terminal_404_from_the_ticket_door(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(404, json={"error": "not found"}))
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 404
    assert "expired or withdrawn" in _body(resp)["error"]


async def test_react_invalid_json_body_is_400(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    resp = await _handler(stub_app, _REACT)(_react_request(raw_body=b"{bad"))
    assert resp.status_code == 400
    assert fake_httpx.calls == []


async def test_react_relays_a_fixed_refusal_for_a_non_envelope_door_body(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # A refusal body that is NOT this platform's error envelope (a WAF page, a proxy
    # banner) is replaced by a fixed refusal and never relayed to the anonymous visitor.
    await _seed_question()
    fake_httpx.responses.append(response(400, json={"unexpected": "x"}))
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 400
    assert _body(resp)["error"] == "the reaction was refused"


async def test_react_relays_a_fixed_refusal_for_a_non_json_door_body(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(502, text="<html>gateway</html>"))
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 502
    assert _body(resp)["error"] == "the reaction was refused"


async def test_react_unexpected_status_raises(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_question()
    fake_httpx.responses.append(response(500, text="boom"))
    with pytest.raises(ReactionForwardError):
        await _handler(stub_app, _REACT)(_react_request())


async def test_react_2xx_non_json_body_raises(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    # A 2xx whose body is not even JSON carries no update — a server fault, raised.
    await _seed_question()
    fake_httpx.responses.append(response(200, text="not json"))
    with pytest.raises(ReactionForwardError):
        await _handler(stub_app, _REACT)(_react_request())


async def test_react_2xx_without_update_raises(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    # A 2xx that does not carry a form update is a server fault, raised — never acked as an
    # empty success the widget would silently apply as nothing.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "weird"}}))
    with pytest.raises(ReactionForwardError):
        await _handler(stub_app, _REACT)(_react_request())


async def test_react_from_a_session_on_another_route_is_404(
    web_env, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A session minted on another web route may not react on this one — the same route-scope
    # rule the answer door enforces (a foreign session is indistinguishable from none).
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, OTHER_IDENTITY)
    await _seed_question()
    resp = await _handler(stub_app, _REACT)(_react_request())
    assert resp.status_code == 404
    assert fake_httpx.calls == []


def test_web_channel_advertises_form_reaction():
    from tai42_channel_web.channel import WebChannel

    assert WebChannel.supports_form_reaction is True
