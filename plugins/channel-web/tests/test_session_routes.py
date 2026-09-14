"""The session-rotation door — unregister-and-mint, the no-session-needed rule, the
cross-origin and body refusals, and the rotate-door entry gate."""

from __future__ import annotations

import pytest

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.store.entry_gate import mint_entry_code, set_gate
from tai42_channel_web.store.registrations import resolve_session

from .conftest import (
    _FOREIGN_ORIGIN,
    _ROTATE,
    _ROTATE_URL,
    IDENTITY,
    SECURE_COOKIE,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeRedis,
    _body,
    _handler,
    _rotate_request,
    _set_cookie,
    build_request,
)

pytestmark = pytest.mark.usefixtures("web_env")


async def test_rotate_unregisters_the_old_session_and_mints_a_fresh_one(
    web_env, stub_app, registered_session: FakeRedis
):
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"status": "rotated"}}
    minted = _set_cookie(resp)[SECURE_COOKIE]
    assert minted.value != SESSION_TOKEN
    assert minted["httponly"] is True
    # The old token can never address the old conversation again…
    assert await resolve_session(SESSION_TOKEN) is None
    # …and the new one addresses a brand-new, empty conversation on the same route.
    rotated = await resolve_session(minted.value)
    assert rotated is not None
    assert rotated.visitor_id != VISITOR_ID
    assert rotated.identity == IDENTITY


async def test_rotate_without_a_session_still_mints_one(web_env, stub_app, fake_redis: FakeRedis):
    # Rotation is not a credential check: opening the chat page mints a session to
    # anyone, so requiring one here would refuse what the page hands out for free.
    resp = await _handler(stub_app, _ROTATE)(_rotate_request())
    assert resp.status_code == 200
    assert await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value) is not None


async def test_rotate_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(token=SESSION_TOKEN, extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.visitor_id == VISITOR_ID


async def test_rotate_with_an_unreadable_body_is_400(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ROTATE)(build_request(path=_ROTATE_URL, raw_body=b"{bad", token=SESSION_TOKEN))

    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid JSON body"
    # Nothing was rotated: the session it was asked to replace still resolves.
    assert await resolve_session(SESSION_TOKEN) is not None


async def test_rotate_unconfigured_store_is_501(no_web_env, stub_app):
    # The store guard stands ahead of the body: without a registration store there is
    # nothing to mint into, whatever the body says.
    resp = await _handler(stub_app, _ROTATE)(build_request(path=_ROTATE_URL, token=SESSION_TOKEN))
    assert resp.status_code == 501


# -- entry gate (rotate door) ---------------------------------------------------


async def test_rotate_on_a_gated_route_refuses_without_a_live_code(web_env, stub_app, fake_redis: FakeRedis):
    await set_gate(IDENTITY, True)
    handler = _handler(stub_app, _ROTATE)
    missing = await handler(_rotate_request())
    wrong = await handler(build_request(path=_ROTATE_URL, json_body={"identity": IDENTITY, "entry_code": "nope"}))
    assert missing.status_code == wrong.status_code == 403
    assert _body(missing)["code"] == _body(wrong)["code"] == "entry_refused"


async def test_rotate_on_a_gated_route_admits_a_live_code_and_clears_params(web_env, stub_app, fake_redis: FakeRedis):
    await set_gate(IDENTITY, True)
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    resp = await _handler(stub_app, _ROTATE)(
        build_request(path=_ROTATE_URL, json_body={"identity": IDENTITY, "entry_code": raw_code})
    )
    assert resp.status_code == 200
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.params == {}


async def test_rotate_on_an_ungated_route_ignores_the_code_field(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _ROTATE)(
        build_request(path=_ROTATE_URL, json_body={"identity": IDENTITY, "entry_code": "irrelevant"})
    )
    assert resp.status_code == 200
