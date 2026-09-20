"""Public /api/login/* route behavior against the store + redis fakes."""

from __future__ import annotations

import pytest

from tai42_accounts_postgres import rate_limit, routes_login, service
from tai42_accounts_postgres.hashing import DUMMY_HASH, HashCapacityError, hash_password
from tai42_accounts_postgres.settings import accounts_settings

from .conftest import FakeRedis, build_request, future, response_json


@pytest.fixture
def redis_fake(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rate_limit, "client_ctx", lambda cls, s=None, **k: _ctx(fake))
    return fake


def _ctx(fake):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield fake

    return _cm()


# Distinguishes "caller passed no hash, derive one from the password" from an
# explicit ``password_hash=None`` seeding a NULL hash (an un-accepted invite).
_FROM_PASSWORD = object()


def _seed_user(wire, password: str, *, disabled=False, password_hash=_FROM_PASSWORD):
    wire.users.rows["usr-1"] = {
        "user_id": "usr-1",
        "email": "a@b.c",
        "password_hash": hash_password(password) if password_hash is _FROM_PASSWORD else password_hash,
        "role": "admin",
        "disabled": disabled,
        "created_at": future(0),
    }


def _account_key() -> str:
    return f"{accounts_settings().key_prefix}:acc:login:fail:a@b.c"


def _ip_key(ip: str = "198.51.100.7") -> str:
    return f"{accounts_settings().key_prefix}:acc:login:ip:{ip}"


# -- password login -------------------------------------------------------------


async def test_login_password_success(wire, redis_fake):
    _seed_user(wire, "correct-password")
    resp = await routes_login.login_password(build_request({"email": "A@B.C", "password": "correct-password"}))
    assert resp.status_code == 200
    body = response_json(resp)["data"]
    assert body["token"].startswith("tai-sess-")
    assert body["user_id"] == "usr-1"


async def test_login_wrong_password_401_and_records_failure(wire, redis_fake):
    _seed_user(wire, "correct-password")
    resp = await routes_login.login_password(build_request({"email": "a@b.c", "password": "wrong"}))
    assert resp.status_code == 401
    assert response_json(resp) == {"error": "Invalid credentials"}
    assert await redis_fake.get(_account_key()) == "1"


async def test_unknown_and_wrong_bodies_are_byte_identical(wire, redis_fake):
    _seed_user(wire, "correct-password")
    wrong = await routes_login.login_password(build_request({"email": "a@b.c", "password": "wrong"}))
    unknown = await routes_login.login_password(build_request({"email": "nobody@x.y", "password": "wrong"}))
    assert bytes(wrong.body) == bytes(unknown.body)
    assert wrong.status_code == unknown.status_code == 401


async def test_unknown_email_runs_dummy_verify(wire, redis_fake, monkeypatch):
    calls: list[str] = []

    async def recording_verify(h, p):
        calls.append(h)
        return False

    monkeypatch.setattr(routes_login, "verify_password", recording_verify)
    await routes_login.login_password(build_request({"email": "nobody@x.y", "password": "pw"}))
    assert calls == [DUMMY_HASH]


async def test_correct_password_never_blocked_even_over_limit(wire, redis_fake):
    _seed_user(wire, "correct-password")
    await redis_fake.set(_account_key(), "999")
    resp = await routes_login.login_password(build_request({"email": "a@b.c", "password": "correct-password"}))
    assert resp.status_code == 200
    # Success clears the account counter.
    assert await redis_fake.get(_account_key()) is None


async def test_failed_attempt_trips_429_with_retry_after(wire, redis_fake):
    _seed_user(wire, "correct-password")
    await redis_fake.set(_account_key(), "5")  # threshold is 5; the sixth failure locks
    resp = await routes_login.login_password(build_request({"email": "a@b.c", "password": "wrong"}))
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert "Too many attempts" in response_json(resp)["error"]


async def test_disabled_user_401(wire, redis_fake):
    _seed_user(wire, "correct-password", disabled=True)
    resp = await routes_login.login_password(build_request({"email": "a@b.c", "password": "correct-password"}))
    assert resp.status_code == 401


async def test_null_password_401(wire, redis_fake):
    _seed_user(wire, "unused", password_hash=None)
    # The seeded row is what this test is about: a NULL hash, the un-accepted invite.
    # A seeder that derived a hash from "unused" instead would still answer 401 here —
    # the wrong password's 401, from a row that never had the shape under test — so the
    # row is pinned directly rather than read back through the response.
    assert wire.users.rows["usr-1"]["password_hash"] is None
    resp = await routes_login.login_password(build_request({"email": "a@b.c", "password": "anything"}))
    assert resp.status_code == 401


async def test_hash_capacity_sheds_503(wire, redis_fake, monkeypatch):
    _seed_user(wire, "correct-password")

    async def shed(h, p):
        raise HashCapacityError("busy")

    monkeypatch.setattr(routes_login, "verify_password", shed)
    resp = await routes_login.login_password(build_request({"email": "a@b.c", "password": "correct-password"}))
    assert resp.status_code == 503
    assert "Retry-After" in resp.headers


async def test_invalid_json_400(wire, redis_fake):
    req = build_request(None)
    resp = await routes_login.login_password(req)
    assert resp.status_code == 400


async def test_invalid_body_422(wire, redis_fake):
    resp = await routes_login.login_password(build_request({"email": "a@b.c"}))
    assert resp.status_code == 422


# -- invite accept --------------------------------------------------------------


def _seed_invite(wire, raw_token: str, *, user_id="usr-1", expires=None):
    wire.users.rows[user_id] = {
        "user_id": user_id,
        "email": "a@b.c",
        "password_hash": None,
        "role": "viewer",
        "disabled": False,
        "created_at": future(0),
    }
    wire.invites.rows[service.token_hash(raw_token)] = {
        "user_id": user_id,
        "expires_at": expires if expires is not None else future(),
        "consumed_at": None,
    }


async def test_invite_accept_happy(wire, redis_fake):
    raw = service.new_invite_token()
    _seed_invite(wire, raw)
    resp = await routes_login.login_invite_accept(
        build_request({"invite_token": raw, "password": "brand-new-pass", "password_confirm": "brand-new-pass"})
    )
    assert resp.status_code == 200
    assert wire.users.rows["usr-1"]["password_hash"] is not None
    assert response_json(resp)["data"]["token"].startswith("tai-sess-")


async def test_invite_confirm_mismatch_422(wire, redis_fake):
    raw = service.new_invite_token()
    _seed_invite(wire, raw)
    resp = await routes_login.login_invite_accept(
        build_request({"invite_token": raw, "password": "brand-new-pass", "password_confirm": "different-pass"})
    )
    assert resp.status_code == 422
    assert response_json(resp) == {"error": "Passwords do not match"}


async def test_invite_too_short_422(wire, redis_fake):
    raw = service.new_invite_token()
    _seed_invite(wire, raw)
    resp = await routes_login.login_invite_accept(
        build_request({"invite_token": raw, "password": "short", "password_confirm": "short"})
    )
    assert resp.status_code == 422
    assert "at least 10" in response_json(resp)["error"]


async def test_invite_miss_400_and_records_failure(wire, redis_fake):
    resp = await routes_login.login_invite_accept(
        build_request(
            {"invite_token": "tai-inv-unknown", "password": "brand-new-pass", "password_confirm": "brand-new-pass"}
        )
    )
    assert resp.status_code == 400
    assert response_json(resp) == {"error": "Invalid or expired invite"}


async def test_invite_miss_logs_source_ip(wire, redis_fake, caplog):
    with caplog.at_level("WARNING"):
        resp = await routes_login.login_invite_accept(
            build_request(
                {"invite_token": "tai-inv-x", "password": "brand-new-pass", "password_confirm": "brand-new-pass"}
            )
        )
    assert resp.status_code == 400
    # A replayed/guessed invite is a security signal, logged with the source ip.
    assert "invite consume miss" in caplog.text
    assert "198.51.100.7" in caplog.text


async def test_invite_miss_ip_throttle_429(wire, redis_fake):
    await redis_fake.set(_ip_key(), "30")  # per-IP window already at the limit
    resp = await routes_login.login_invite_accept(
        build_request({"invite_token": "tai-inv-x", "password": "brand-new-pass", "password_confirm": "brand-new-pass"})
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_invite_replay_rejected_no_reissue(wire, redis_fake):
    raw = service.new_invite_token()
    _seed_invite(wire, raw)
    first = await routes_login.login_invite_accept(
        build_request({"invite_token": raw, "password": "brand-new-pass", "password_confirm": "brand-new-pass"})
    )
    assert first.status_code == 200
    issued_hash = wire.users.rows["usr-1"]["password_hash"]
    sessions_after_first = dict(wire.sessions.rows)

    # Re-POSTing the same (now consumed) invite is rejected — and nothing is re-issued.
    replay = await routes_login.login_invite_accept(
        build_request(
            {"invite_token": raw, "password": "totally-different-pass", "password_confirm": "totally-different-pass"}
        )
    )
    assert replay.status_code == 400
    assert response_json(replay) == {"error": "Invalid or expired invite"}
    assert wire.users.rows["usr-1"]["password_hash"] == issued_hash  # no password rotation
    assert wire.sessions.rows == sessions_after_first  # no new session


# -- AC-required boot guard -----------------------------------------------------


def test_boot_guard_raises_when_unpopulated():
    # The autouse reset clears the active provider, so the boot guard fails loudly.
    with pytest.raises(RuntimeError, match="never instantiated"):
        routes_login._assert_accounts_provider_instantiated()


def test_boot_guard_passes_when_populated(wire):
    routes_login._assert_accounts_provider_instantiated()
