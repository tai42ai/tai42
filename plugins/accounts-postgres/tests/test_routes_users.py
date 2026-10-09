"""The self-service /api/auth/users/me/password route behavior against the fakes."""

from __future__ import annotations

import pytest
from tai42_contract.access_control.context import set_request_user_id

from tai42_accounts_postgres import routes_users, service
from tai42_accounts_postgres.hashing import hash_password

from .conftest import build_request, future, gate_verified


@pytest.fixture
def as_user():
    # Set the request-scoped caller id inside the running test context; clear it on
    # teardown with a fresh set(None) (a Token cannot be reset across contexts).
    def _set(user_id: str) -> None:
        set_request_user_id(user_id)

    yield _set
    set_request_user_id(None)


def _add_user(wire, user_id, *, email=None, disabled=False, password_hash=None):
    wire.users.rows[user_id] = {
        "user_id": user_id,
        "email": email or f"{user_id}@x.y",
        "password_hash": password_hash,
        "disabled": disabled,
        "created_at": future(0),
    }


# -- self password change -------------------------------------------------------


async def test_change_own_password_keeps_presented_session(wire, as_user):
    raw = service.new_session_token()
    presented_hash = service.token_hash(raw)
    _add_user(wire, "usr-1", password_hash=hash_password("old-password-1"))
    wire.sessions.rows[presented_hash] = {
        "user_id": "usr-1",
        "last_seen_at": future(0),
        "absolute_expires_at": future(),
    }
    wire.sessions.rows["other"] = {"user_id": "usr-1", "last_seen_at": future(0), "absolute_expires_at": future()}
    as_user("usr-1")
    gate_verified(raw)

    resp = await routes_users.change_own_password(
        build_request(
            {"current_password": "old-password-1", "new_password": "brand-new-password"},
            method="PUT",
            headers={"Authorization": f"Bearer {raw}"},
        )
    )
    assert resp.status_code == 200
    assert presented_hash in wire.sessions.rows  # survives
    assert "other" not in wire.sessions.rows  # revoked
    assert wire.users.rows["usr-1"]["password_hash"] != hash_password("old-password-1")


async def test_change_own_password_spares_the_session_the_gate_verified(wire, as_user):
    # A stale session in Authorization and the live one in X-Api-Key: the gate verified the
    # live one, so it is the session spared; the stale one is revoked with the rest.
    stale, live = service.new_session_token(), service.new_session_token()
    stale_hash, live_hash = service.token_hash(stale), service.token_hash(live)
    _add_user(wire, "usr-1", password_hash=hash_password("old-password-1"))
    for token_hash in (stale_hash, live_hash):
        wire.sessions.rows[token_hash] = {
            "user_id": "usr-1",
            "last_seen_at": future(0),
            "absolute_expires_at": future(),
        }
    as_user("usr-1")
    gate_verified(live)

    resp = await routes_users.change_own_password(
        build_request(
            {"current_password": "old-password-1", "new_password": "brand-new-password"},
            method="PUT",
            headers={"Authorization": f"Bearer {stale}", "X-Api-Key": live},
        )
    )
    assert resp.status_code == 200
    assert live_hash in wire.sessions.rows
    assert stale_hash not in wire.sessions.rows


async def test_change_own_password_with_a_key_revokes_every_session(wire, as_user):
    # Verified by an api key (not a session): no session is spared.
    _add_user(wire, "usr-1", password_hash=hash_password("old-password-1"))
    wire.sessions.rows["one"] = {"user_id": "usr-1", "last_seen_at": future(0), "absolute_expires_at": future()}
    as_user("usr-1")
    gate_verified("sk-not-a-session")

    resp = await routes_users.change_own_password(
        build_request({"current_password": "old-password-1", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 200
    assert "one" not in wire.sessions.rows


async def test_change_own_password_wrong_current_403(wire, as_user):
    _add_user(wire, "usr-1", password_hash=hash_password("old-password-1"))
    as_user("usr-1")
    resp = await routes_users.change_own_password(
        build_request({"current_password": "wrong", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 403


async def test_change_own_password_too_short_422(wire, as_user):
    _add_user(wire, "usr-1", password_hash=hash_password("old-password-1"))
    as_user("usr-1")
    resp = await routes_users.change_own_password(
        build_request({"current_password": "old-password-1", "new_password": "short"}, method="PUT")
    )
    assert resp.status_code == 422


async def test_change_own_password_invalid_body_422(wire, as_user):
    _add_user(wire, "usr-1", password_hash=hash_password("old-password-1"))
    as_user("usr-1")
    resp = await routes_users.change_own_password(build_request({"current_password": "old-password-1"}, method="PUT"))
    assert resp.status_code == 422


async def test_change_own_password_unauthenticated_401(wire):
    resp = await routes_users.change_own_password(
        build_request({"current_password": "x", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 401


async def test_change_own_password_no_password_set_400(wire, as_user):
    _add_user(wire, "usr-1", password_hash=None)
    as_user("usr-1")
    resp = await routes_users.change_own_password(
        build_request({"current_password": "x", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 400
