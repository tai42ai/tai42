"""Authed /api/auth/users* route behavior against the store + services fakes."""

from __future__ import annotations

import pytest
from tai42_contract.access_control.context import set_request_user_id

from tai42_accounts_postgres import routes_users, service
from tai42_accounts_postgres.hashing import hash_password

from .conftest import build_request, future, response_json


@pytest.fixture
def as_user():
    # Set the request-scoped caller id inside the running test context; clear it on
    # teardown with a fresh set(None) (a Token cannot be reset across contexts).
    def _set(user_id: str) -> None:
        set_request_user_id(user_id)

    yield _set
    set_request_user_id(None)


def _add_user(wire, user_id, *, email=None, role="viewer", disabled=False, password_hash=None):
    wire.users.rows[user_id] = {
        "user_id": user_id,
        "email": email or f"{user_id}@x.y",
        "password_hash": password_hash,
        "role": role,
        "disabled": disabled,
        "created_at": future(0),
    }


# -- list -----------------------------------------------------------------------


async def test_list_users(wire):
    _add_user(wire, "usr-1", role="admin", password_hash="h")
    _add_user(wire, "usr-2", role="viewer")
    resp = await routes_users.list_users(build_request(method="GET"))
    users = response_json(resp)["data"]["users"]
    assert {u["user_id"] for u in users} == {"usr-1", "usr-2"}
    pending = {u["user_id"]: u["pending_invite"] for u in users}
    assert pending == {"usr-1": False, "usr-2": True}
    # Never leaks a hash.
    assert all("password_hash" not in u for u in users)


# -- create ---------------------------------------------------------------------


async def test_create_user_returns_login_path_once(wire):
    resp = await routes_users.create_user(build_request({"email": "New@X.Y", "role": "viewer"}))
    data = response_json(resp)["data"]
    assert data["invite_token"].startswith("tai-inv-")
    assert data["login_path"] == f"/login?invite={data['invite_token']}"
    assert ("apply_role", data["user_id"], "viewer") in wire.admin.calls
    # The email was normalized before storage.
    assert wire.users.rows[data["user_id"]]["email"] == "new@x.y"


async def test_create_user_email_taken_409(wire):
    _add_user(wire, "usr-1", email="dup@x.y")
    resp = await routes_users.create_user(build_request({"email": "dup@x.y", "role": "viewer"}))
    assert resp.status_code == 409


async def test_create_user_apply_role_failure_compensates(wire):
    wire.admin.fail_apply_role = True
    with pytest.raises(RuntimeError, match="apply_role boom"):
        await routes_users.create_user(build_request({"email": "new@x.y", "role": "viewer"}))
    # The half-created user (and any invite) was cleaned up — re-runnable.
    assert wire.users.rows == {}
    assert wire.invites.rows == {}


async def test_create_user_unknown_role_400_and_compensates(wire):
    # Unknown role: apply_role's KeyError surfaces as a 400; cleanup leaves nothing.
    wire.admin.known_roles = {"admin", "editor", "viewer"}
    resp = await routes_users.create_user(build_request({"email": "new@x.y", "role": "bogus"}))
    assert resp.status_code == 400
    assert wire.users.rows == {}
    assert wire.invites.rows == {}


# -- update ---------------------------------------------------------------------


async def test_update_role_applies_template(wire):
    _add_user(wire, "usr-1", role="admin")  # another admin so the guard passes
    _add_user(wire, "usr-2", role="viewer")
    resp = await routes_users.update_user(build_request({"role": "admin"}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert ("apply_role", "usr-2", "admin") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "admin"


async def test_update_user_unknown_role_400(wire):
    # Unknown role on update → 400; the target keeps its prior role (nothing written).
    wire.admin.known_roles = {"admin", "editor", "viewer"}
    _add_user(wire, "usr-1", role="admin")  # another admin so the guard would pass
    _add_user(wire, "usr-2", role="viewer")
    resp = await routes_users.update_user(build_request({"role": "bogus"}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 400
    assert wire.users.rows["usr-2"]["role"] == "viewer"


async def test_demote_admin_allowed_when_another_admin_remains(wire):
    _add_user(wire, "usr-1", role="admin")  # the surviving admin
    _add_user(wire, "usr-2", role="admin")
    resp = await routes_users.update_user(build_request({"role": "viewer"}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert ("apply_role", "usr-2", "viewer") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "viewer"


async def test_disable_non_admin_needs_no_guard(wire):
    _add_user(wire, "usr-2", role="viewer")
    wire.sessions.rows["th"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    resp = await routes_users.update_user(build_request({"disabled": True}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert "th" not in wire.sessions.rows
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_disable_kills_credentials_first(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")
    wire.sessions.rows["th-2"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    resp = await routes_users.update_user(build_request({"disabled": True}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert "th-2" not in wire.sessions.rows  # session revoked
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_reenable_reverses_order(wire):
    _add_user(wire, "usr-2", role="viewer", disabled=True)
    resp = await routes_users.update_user(build_request({"disabled": False}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert wire.users.rows["usr-2"]["disabled"] is False
    assert ("set_user_disabled", "usr-2", "False") in wire.admin.calls


async def test_combined_demote_and_reenable_applies_both(wire):
    # A disabled admin demoted AND re-enabled in one request; neither half is dropped.
    _add_user(wire, "usr-1", role="admin")  # the surviving admin
    _add_user(wire, "usr-2", role="admin", disabled=True)
    resp = await routes_users.update_user(
        build_request({"role": "user", "disabled": False}, path_params={"user_id": "usr-2"})
    )
    assert resp.status_code == 200
    # Role demoted...
    assert ("apply_role", "usr-2", "user") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "user"
    # ...AND re-enabled (the previously silently-dropped half).
    assert ("set_user_disabled", "usr-2", "False") in wire.admin.calls
    assert wire.users.rows["usr-2"]["disabled"] is False


async def test_combined_role_and_disable_applies_both(wire):
    # The mirror direction: a role change combined with disable(true) still disables.
    _add_user(wire, "usr-1", role="admin")  # keeps an admin alive
    _add_user(wire, "usr-2", role="admin")
    resp = await routes_users.update_user(
        build_request({"role": "user", "disabled": True}, path_params={"user_id": "usr-2"})
    )
    assert resp.status_code == 200
    assert ("apply_role", "usr-2", "user") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "user"
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_cannot_disable_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    resp = await routes_users.update_user(build_request({"disabled": True}, path_params={"user_id": "usr-1"}))
    assert resp.status_code == 409


async def test_cannot_demote_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    resp = await routes_users.update_user(build_request({"role": "viewer"}, path_params={"user_id": "usr-1"}))
    assert resp.status_code == 409


async def test_concurrent_admin_removals_cannot_both_orphan(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")

    # A concurrent request disabled the other admin before this disable took the
    # lock; the under-lock count sees zero others, so this one refuses.
    def _other_disables_usr2(store) -> None:
        store.rows["usr-2"]["disabled"] = True

    wire.users.admin_guard_hook = _other_disables_usr2
    resp = await routes_users.update_user(build_request({"disabled": True}, path_params={"user_id": "usr-1"}))
    assert resp.status_code == 409
    # usr-1 stayed enabled — the two removals never both landed to leave zero admins.
    assert wire.users.rows["usr-1"]["disabled"] is False


async def test_demote_reevaluates_disabled_under_lock(wire):
    # Pre-lock snapshot shows usr-1 disabled, but a concurrent request re-enabled it
    # and disabled usr-2; the under-lock re-read counts zero others and refuses.
    _add_user(wire, "usr-1", role="admin", disabled=True)
    _add_user(wire, "usr-2", role="admin")

    def _reenable_usr1_disable_usr2(store) -> None:
        store.rows["usr-1"]["disabled"] = False
        store.rows["usr-2"]["disabled"] = True

    wire.users.admin_guard_hook = _reenable_usr1_disable_usr2
    resp = await routes_users.update_user(build_request({"role": "viewer"}, path_params={"user_id": "usr-1"}))
    assert resp.status_code == 409
    assert wire.users.rows["usr-1"]["role"] == "admin"  # not demoted


async def test_concurrent_admin_delete_refused_when_other_removed(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")

    def _other_demotes_usr2(store) -> None:
        store.rows["usr-2"]["role"] = "viewer"

    wire.users.admin_guard_hook = _other_demotes_usr2
    resp = await routes_users.delete_user(build_request(method="DELETE", path_params={"user_id": "usr-1"}))
    assert resp.status_code == 409
    assert "usr-1" in wire.users.rows  # not deleted


async def test_update_target_vanishes_under_lock_404(wire):
    # A concurrent delete removed the row before the lock; the under-lock re-read
    # returns None, so the mutation 404s rather than acting on a ghost.
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="viewer")

    def _delete_usr2(store) -> None:
        store.rows.pop("usr-2", None)

    wire.users.admin_guard_hook = _delete_usr2
    resp = await routes_users.update_user(build_request({"role": "admin"}, path_params={"user_id": "usr-2"}))
    assert resp.status_code == 404


async def test_update_unknown_user_404(wire):
    resp = await routes_users.update_user(build_request({"role": "viewer"}, path_params={"user_id": "ghost"}))
    assert resp.status_code == 404


# -- delete ---------------------------------------------------------------------


async def test_delete_user_orders_policy_first(wire):
    _add_user(wire, "usr-1", role="admin")  # keeps an admin alive
    _add_user(wire, "usr-2", role="viewer", password_hash="h")
    wire.sessions.rows["th"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    wire.invites.rows["ih"] = {"user_id": "usr-2", "expires_at": future(), "consumed_at": None}
    resp = await routes_users.delete_user(build_request(method="DELETE", path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert ("remove_policy", "usr-2") in wire.admin.calls
    assert "usr-2" not in wire.users.rows
    assert wire.sessions.rows == {}
    assert wire.invites.rows == {}


async def test_delete_admin_allowed_when_another_admin_remains(wire):
    _add_user(wire, "usr-1", role="admin", password_hash="h")
    _add_user(wire, "usr-2", role="admin")  # the surviving admin
    wire.sessions.rows["th-1"] = {"user_id": "usr-1", "last_seen_at": future(0), "absolute_expires_at": future()}
    wire.invites.rows["ih-1"] = {"user_id": "usr-1", "expires_at": future(), "consumed_at": None}
    resp = await routes_users.delete_user(build_request(method="DELETE", path_params={"user_id": "usr-1"}))
    assert resp.status_code == 200
    assert ("remove_policy", "usr-1") in wire.admin.calls
    assert "usr-1" not in wire.users.rows
    assert wire.sessions.rows == {}  # usr-1's sessions revoked on the guard connection
    assert wire.invites.rows == {}  # usr-1's invites revoked on the guard connection


async def test_cannot_delete_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    resp = await routes_users.delete_user(build_request(method="DELETE", path_params={"user_id": "usr-1"}))
    assert resp.status_code == 409


async def test_delete_unknown_404(wire):
    resp = await routes_users.delete_user(build_request(method="DELETE", path_params={"user_id": "ghost"}))
    assert resp.status_code == 404


# -- regenerate invite ----------------------------------------------------------


async def test_regenerate_invite_for_pending_user(wire):
    _add_user(wire, "usr-2", role="viewer", password_hash=None)
    resp = await routes_users.regenerate_invite(build_request(path_params={"user_id": "usr-2"}))
    assert resp.status_code == 200
    assert response_json(resp)["data"]["invite_token"].startswith("tai-inv-")


async def test_regenerate_invite_409_when_password_set(wire):
    _add_user(wire, "usr-2", role="viewer", password_hash="h")
    resp = await routes_users.regenerate_invite(build_request(path_params={"user_id": "usr-2"}))
    assert resp.status_code == 409


async def test_regenerate_invite_unknown_404(wire):
    resp = await routes_users.regenerate_invite(build_request(path_params={"user_id": "ghost"}))
    assert resp.status_code == 404


# -- self password change -------------------------------------------------------


async def test_change_own_password_keeps_presented_session(wire, as_user):
    raw = service.new_session_token()
    presented_hash = service.token_hash(raw)
    _add_user(wire, "usr-1", role="admin", password_hash=hash_password("old-password-1"))
    wire.sessions.rows[presented_hash] = {
        "user_id": "usr-1",
        "last_seen_at": future(0),
        "absolute_expires_at": future(),
    }
    wire.sessions.rows["other"] = {"user_id": "usr-1", "last_seen_at": future(0), "absolute_expires_at": future()}
    as_user("usr-1")

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


async def test_change_own_password_wrong_current_403(wire, as_user):
    _add_user(wire, "usr-1", role="admin", password_hash=hash_password("old-password-1"))
    as_user("usr-1")
    resp = await routes_users.change_own_password(
        build_request({"current_password": "wrong", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 403


async def test_change_own_password_too_short_422(wire, as_user):
    _add_user(wire, "usr-1", role="admin", password_hash=hash_password("old-password-1"))
    as_user("usr-1")
    resp = await routes_users.change_own_password(
        build_request({"current_password": "old-password-1", "new_password": "short"}, method="PUT")
    )
    assert resp.status_code == 422


async def test_change_own_password_unauthenticated_401(wire):
    resp = await routes_users.change_own_password(
        build_request({"current_password": "x", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 401


async def test_change_own_password_no_password_set_400(wire, as_user):
    _add_user(wire, "usr-1", role="admin", password_hash=None)
    as_user("usr-1")
    resp = await routes_users.change_own_password(
        build_request({"current_password": "x", "new_password": "brand-new-password"}, method="PUT")
    )
    assert resp.status_code == 400
