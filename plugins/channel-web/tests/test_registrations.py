"""Session registrations — register/resolve/drop, TTL promotion, the strict decode,
and captured link params."""

from __future__ import annotations

import json

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_channel_web.store.registrations import (
    SessionRecordError,
    SessionRegistration,
    drop_session,
    register_session,
    resolve_session,
    update_session_params,
)

from .conftest import IDENTITY, SESSION_TOKEN, VISITOR_ID, FakeRedis

pytestmark = pytest.mark.usefixtures("web_env")

_SESSION_KEY = f"channel:web:session:{SESSION_TOKEN}"


async def test_register_then_resolve_returns_the_address_and_its_route(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})

    assert json.loads(fake_redis.store[_SESSION_KEY])["visitor_id"] == VISITOR_ID
    # The route the session was minted on rides the registration: it is what binds
    # this token to one conversation surface.
    assert json.loads(fake_redis.store[_SESSION_KEY])["identity"] == IDENTITY
    assert await resolve_session(SESSION_TOKEN) == SessionRegistration(visitor_id=VISITOR_ID, identity=IDENTITY)


async def test_a_fresh_mint_only_gets_the_short_pending_ttl(fake_redis: FakeRedis):
    # Nothing has come back with this cookie yet: an anonymous GET loop must leave
    # minute-lived keys behind, not one 30-day registration per request.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    assert fake_redis.ttls[_SESSION_KEY] == 600


async def test_resolving_promotes_a_pending_registration_to_the_full_ttl(fake_redis: FakeRedis):
    # The cookie came back, so this is a real visitor — from here the registration
    # lives as long as any other session.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})

    assert await resolve_session(SESSION_TOKEN) is not None
    assert fake_redis.ttls[_SESSION_KEY] == 30 * 86400


async def test_resolve_refreshes_the_registration_ttl(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    fake_redis.ttls[_SESSION_KEY] = 5
    monkeypatch.setenv("CHANNEL_WEB_SESSION_TTL_SECONDS", "600")
    reset_all_settings()

    assert await resolve_session(SESSION_TOKEN) is not None
    assert fake_redis.ttls[_SESSION_KEY] == 600


async def test_resolve_reads_and_expires_in_one_round_trip(fake_redis: FakeRedis):
    # GETEX, not GET+EXPIRE: every door that touches a cookie resolves a session, so
    # the second round trip would be paid on every request.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    fake_redis.events.clear()

    await resolve_session(SESSION_TOKEN)

    assert fake_redis.events == [("redis_getex", _SESSION_KEY)]


async def test_resolve_unregistered_token_is_none(fake_redis: FakeRedis):
    # The invented-cookie case: a shape-valid token nobody registered is no session,
    # so it can never become a conversation address.
    assert await resolve_session(SESSION_TOKEN) is None


@pytest.mark.parametrize(
    ("stored", "missing"),
    [
        ({"created_at": "now", "identity": IDENTITY, "params": {}}, "visitor_id"),
        ({"created_at": "now", "visitor_id": VISITOR_ID, "params": {}}, "identity"),
        # No old-format tolerance: a record predating link params (no ``params`` key)
        # fails loud and the visitor re-mints, rather than being silently defaulted.
        ({"created_at": "now", "visitor_id": VISITOR_ID, "identity": IDENTITY}, "params"),
        ({"visitor_id": VISITOR_ID, "identity": IDENTITY, "params": "x"}, "params"),
        ({"visitor_id": VISITOR_ID, "identity": IDENTITY, "params": {"k": 1}}, "params"),
    ],
)
async def test_resolve_malformed_registration_raises(fake_redis: FakeRedis, stored: dict, missing: str):
    # Every half is load-bearing — the address a door speaks for, the route it may
    # speak on, and the captured params map — so a record missing or mis-typing any is
    # a loud fault, never a session.
    fake_redis.store[_SESSION_KEY] = json.dumps(stored)
    with pytest.raises(SessionRecordError, match=missing):
        await resolve_session(SESSION_TOKEN)


async def test_register_captures_and_resolves_params(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "spring", "n": "3"})
    stored = json.loads(fake_redis.store[_SESSION_KEY])
    assert stored["params"] == {"ref": "spring", "n": "3"}
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.params == {"ref": "spring", "n": "3"}


async def test_a_fresh_mint_without_params_still_carries_the_empty_map(fake_redis: FakeRedis):
    # The key is ALWAYS present — the strict decode requires it, so there is no
    # absent-key shape to tolerate.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    assert json.loads(fake_redis.store[_SESSION_KEY])["params"] == {}


async def test_update_session_params_rewrites_at_the_full_ttl(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "old"})
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    await update_session_params(SESSION_TOKEN, registration, {"ref": "new"})
    stored = json.loads(fake_redis.store[_SESSION_KEY])
    # Same token, same visitor id, new params — and the cookie came back, so a real
    # visitor gets the FULL session TTL.
    assert stored["visitor_id"] == VISITOR_ID
    assert stored["identity"] == IDENTITY
    assert stored["params"] == {"ref": "new"}
    assert fake_redis.ttls[_SESSION_KEY] == 30 * 86400


async def test_drop_session_unregisters_the_token(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    await drop_session(SESSION_TOKEN)
    assert await resolve_session(SESSION_TOKEN) is None
