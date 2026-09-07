"""A REAL Postgres exercise of the access-control policy store: the two ``UNIQUE``
constraints as the durable authority (``user_id`` on policies, ``url`` on routes), the
scope-strip cascade running as genuine ``array_remove`` over a ``TEXT[]`` column with the
``%s = ANY(scopes)`` match, the role-pointer count through the real ``policy_data ->> key``
JSON operator, and the grant-vs-remove lock coupling under true MVCC (``FOR SHARE`` on the
route rows vs the removal's exclusive delete) — none of which a fake can exhibit.

It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a
live Postgres. Without the opt-in the tests SKIP VISIBLY with a clear reason (never a
silent skip)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import LiteralString

import pytest
from psycopg.errors import UniqueViolation
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.access_control.roles import ROLE_POINTER_KEY
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.access_control.store import PostgresAccessControlStore
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[PostgresAccessControlStore, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres access-control store test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs UNIQUE constraints + "
            "array_remove + row locking — no fake)"
        )
    # This suite's autouse ``_default_database`` pins the password to a fake value for the
    # offline transport; under the real-Postgres opt-in it stands down (see conftest), so the
    # operator-provided TAI_DATABASE_DEFAULT_PG_* is honored — rebuild the cached settings to
    # read it.
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    token = f"it_{uuid.uuid4().hex[:12]}"
    await _wipe(token)
    yield PostgresAccessControlStore(), token
    await _wipe(token)
    await shutdown_all_clients()


async def _wipe(token: str) -> None:
    """Delete every row this run wrote — routes/policies carry the per-run token in their
    url / scope_id / user_id — so a shared database stays isolated and a re-run starts clean."""
    await _exec("DELETE FROM access_control_routes WHERE url LIKE %s OR scope_id LIKE %s", (f"%{token}%", f"%{token}%"))
    await _exec("DELETE FROM access_control_policies WHERE user_id LIKE %s", (f"%{token}%",))


async def test_policy_user_id_unique_constraint_is_the_authority(store: tuple[PostgresAccessControlStore, str]) -> None:
    s, token = store
    user = f"{token}-u1"
    await s.create_policy(user, [])
    # The durable ``UNIQUE (user_id)`` rejects a racing second mint of the same user — the
    # real constraint raises, not an in-process pre-check.
    with pytest.raises(UniqueViolation):
        await s.create_policy(user, [])
    assert await s.policy_exists(user) is True


async def test_route_url_unique_upsert_repoints_in_place(store: tuple[PostgresAccessControlStore, str]) -> None:
    s, token = store
    url = f"/{token}/x"
    await s.add_url_to_scope(f"scope-{token}-a", url, pattern=rf"/{token}/x/\d+")
    # ``ON CONFLICT (url)`` against the real ``UNIQUE (url)`` re-points the single row rather
    # than inserting a second: the scope changes and the dropped pattern clears.
    await s.add_url_to_scope(f"scope-{token}-b", url)
    assert await s.fetch_route(url) == f"scope-{token}-b"
    assert await s.get_all_existing_patterns() == {}
    scopes = await s.get_all_existing_scopes()
    assert scopes[url] == f"scope-{token}-b"


async def test_scope_strip_cascade_runs_as_real_array_remove(store: tuple[PostgresAccessControlStore, str]) -> None:
    s, token = store
    scope = f"scope-{token}-a"
    keep = f"scope-{token}-keep"
    user = f"{token}-u1"
    await s.add_url_to_scope(scope, f"/{token}/a")
    await s.add_url_to_scope(keep, f"/{token}/keep")
    await s.create_policy(user, [scope, keep, "*"])
    # ``remove_scope`` deletes the route (its exclusive lock) then strips the scope from every
    # policy in one ``UPDATE ... array_remove(scopes, %s) WHERE %s = ANY(scopes) RETURNING`` —
    # genuine TEXT[] semantics the fake only mimics. The wildcard and the kept scope survive.
    deleted, affected = await s.remove_scope(scope)
    assert deleted == 2  # one route row + one policy stripped
    assert affected == [
        (
            user,
            {
                "scopes": [keep, "*"],
                "policy_data": {},
                "condition": None,
                "condition_id": None,
                "condition_kwargs": None,
            },
        )
    ]
    body = await s.get_policy_body(user)
    assert body is not None
    assert body["scopes"] == [keep, "*"]


async def test_count_policies_with_role_uses_real_json_operator(store: tuple[PostgresAccessControlStore, str]) -> None:
    s, token = store
    role = f"role-{token}"
    other = f"role-{token}-other"
    # The live role pointer lands in ``policy_data`` under ``ROLE_POINTER_KEY``; the count reads
    # it through the real ``policy_data ->> key`` JSON operator, which no fake evaluates.
    await s.create_policy(f"{token}-u1", [], policy_data={ROLE_POINTER_KEY: role})
    await s.create_policy(f"{token}-u2", [], policy_data={ROLE_POINTER_KEY: role})
    await s.create_policy(f"{token}-u3", [], policy_data={ROLE_POINTER_KEY: other})
    assert await s.count_policies_with_role(role, ROLE_POINTER_KEY) == 2
    assert await s.count_policies_with_role(other, ROLE_POINTER_KEY) == 1
    assert await s.count_policies_with_role(f"role-{token}-absent", ROLE_POINTER_KEY) == 0


async def test_remove_url_cascade_reads_last_route_via_any(store: tuple[PostgresAccessControlStore, str]) -> None:
    s, token = store
    scope = f"scope-{token}-a"
    user = f"{token}-u1"
    await s.add_url_to_scope(scope, f"/{token}/a")
    await s.add_url_to_scope(scope, f"/{token}/b")
    await s.create_policy(user, [scope])
    # Two routes back the scope, so dropping one leaves it live (the ``LIMIT 1`` emptiness
    # probe still finds ``/b``): no cascade.
    existed, affected = await s.remove_url_from_scope(f"/{token}/a")
    assert existed is True
    assert affected == []
    body = await s.get_policy_body(user)
    assert body is not None
    assert body["scopes"] == [scope]
    # Dropping the last route empties the scope, so the cascade strips it from the policy.
    existed, affected = await s.remove_url_from_scope(f"/{token}/b")
    assert existed is True
    assert affected == [
        (user, {"scopes": [], "policy_data": {}, "condition": None, "condition_id": None, "condition_kwargs": None})
    ]


async def test_grant_and_remove_serialize_under_row_lock_coupling(
    store: tuple[PostgresAccessControlStore, str],
) -> None:
    """Grant a scope while its last route is being removed, concurrently, against real
    Postgres. The grant locks the route row ``FOR SHARE`` while it validates + writes; the
    removal takes the conflicting exclusive lock when it deletes that row — so the two
    SERIALIZE on genuine MVCC. Whichever wins, the durable invariant holds: no policy keeps a
    scope with no backing route. Either the grant commits first (the removal's post-delete
    cascade then strips the freshly-granted scope) or the removal commits first (the grant's
    ``FOR SHARE`` finds no route and raises ``ValueError`` — no row written)."""
    s, token = store
    scope = f"scope-{token}-race"
    user = f"{token}-u1"
    await s.add_url_to_scope(scope, f"/{token}/race")

    async def _grant() -> None:
        # The removal may commit first: the scope then has no live route, so the grant is
        # fail-closed (``create_policy`` raises ``ValueError``) and writes no row — a durable
        # policy never holds a routeless scope.
        with contextlib.suppress(ValueError):
            await s.create_policy(user, [scope])

    await asyncio.gather(_grant(), s.remove_scope(scope))

    # The route is gone regardless of order, and any policy that landed does NOT carry the
    # removed scope — the exact invariant the lock coupling protects.
    assert scope not in (await s.get_all_existing_scopes()).values()
    body = await s.get_policy_body(user)
    if body is not None:
        assert scope not in body["scopes"]


@pytest.mark.parametrize("user_id_suffix", ["auth0|abc:123", "user@example.com", "naïve-Ünïcode"])
async def test_oidc_subject_and_metachar_scope_round_trip(
    store: tuple[PostgresAccessControlStore, str], user_id_suffix: str
) -> None:
    s, token = store
    user = f"{token}-{user_id_suffix}"
    scope = f"team:{token}@x"
    url = f"/{token}/s"
    # Postgres identities are parameterized COLUMN values, so a subject / scope carrying
    # ``:``/``@``/unicode is a plain bound value with no key-encoding hazard — the real driver
    # round-trips it verbatim, no charset guard.
    await s.add_url_to_scope(scope, url)
    await s.create_policy(user, [scope])
    assert await s.fetch_route(url) == scope
    body = await s.get_policy_body(user)
    assert body is not None
    assert body["scopes"] == [scope]


async def test_settings_public_marker_excluded_by_real_filter(store: tuple[PostgresAccessControlStore, str]) -> None:
    s, token = store
    public = access_control_settings().public_resource_id
    await s.add_url_to_scope(f"scope-{token}-a", f"/{token}/a")
    await s.pin_route_public(f"/{token}/open")
    # The ``scope_id <> marker`` filter runs in SQL: the public row is absent from the scope
    # enumeration but present in the full mapping a backup round-trips.
    scopes = await s.get_all_existing_scopes()
    mappings = await s.get_all_route_mappings()
    assert scopes.get(f"/{token}/a") == f"scope-{token}-a"
    assert f"/{token}/open" not in scopes
    assert mappings[f"/{token}/open"] == public
    assert f"/{token}/open" in await s.get_public_route_pins()
