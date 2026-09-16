"""The caller-capability projection (``GET /api/auth/me``) agrees with the enforced
gate on the deployed stack: for a matrix of identity shapes (the root admin key, a
non-admin human owner's session, its restricted owned key, and an owned key whose
service owner's scopes were shrunk out from under it) the projection NEVER advertises a
door the gate denies, and every real route it omits is one the gate really denies.
Sampled — the exhaustive property is the unit suite's; this proves the running
deployment agrees. The envelope's identity facts — including the owning principal — are
checked against the minting facts."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

from ._owned_support import SCOPE, create_service_owner, mint_key_for, mint_owned, provision_owner

# The seed provisions the admin owner principal (``e2e-owner``) and its root key
# (``e2e-root``, ``*``), so the root key is itself owned by that admin principal.
_ROOT_USER_ID = "e2e-root"
_ROOT_OWNER_ID = "e2e-owner"

# Real GET routes every ``e2e-all`` holder can reach; a scope-stripped identity cannot,
# so they must be ABSENT from its projection AND gate-denied (the reverse direction).
_KNOWN_REAL_GET = ["/api/tools", "/api/notifications", "/api/presets", "/api/schedules", "/api/extensions"]


_MatrixRow = tuple[ApiClient, str, bool, str | None, list[str], str, str]


async def _matrix_rows(owned_keys_stack: TaiStack, uniq: Callable[[str], str]) -> list[_MatrixRow]:
    """Provision the identity matrix and return one row per shape: ``(client, expected
    user_id, expected admin, expected owner_user_id, expected scopes, expected principal
    user_id, expected principal kind)``. The shapes: the root admin key, a non-admin human
    owner's session, its restricted owned key, and an owned key whose service owner's scopes
    were shrunk out from under it."""
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    owner_id, session = await provision_owner(owned_keys_stack, root, uniq, scopes=[SCOPE])
    owner = root.with_token(session)
    owned_id, owned_raw = await mint_owned(owner, uniq)
    owned = root.with_token(owned_raw)

    # The 4th shape: an owned key whose service OWNER's scopes are shrunk out from under
    # it, so its effective (owner-attenuated) scope set empties by PROPAGATION — the real
    # shrink path, not a scope-stripped mint. Minted with e2e-all under a service owner,
    # then that owner is shrunk to a fresh unrelated scope (registered first, as a scope
    # must exist before it can be assigned); the owned key keeps e2e-all but its owner no
    # longer holds it, so the request-time intersection — and thus the projection —
    # collapses to only the authenticated carve-out.
    svc_id = await create_service_owner(root, uniq)
    shrunk_id, shrunk_raw = await mint_key_for(root, uniq, svc_id, scopes=[SCOPE])
    shrunk = root.with_token(shrunk_raw)
    # Sentinel: before the shrink the owned key's projection still carries e2e-all.
    assert sorted((await shrunk.get("/api/auth/me"))["scopes"]) == [SCOPE]
    other_scope = uniq("scope")
    await root.post("/api/auth/scopes", json={"scope_id": other_scope, "url": f"/e2e/{other_scope}"})
    await root.put(f"/api/auth/api-keys/{svc_id}", json={"scopes": [other_scope]})

    async def shrunk_projection_empty() -> bool:
        return (await shrunk.get("/api/auth/me"))["scopes"] == []

    await wait_for_async(
        shrunk_projection_empty,
        deadline=5.0,
        message="owner scope shrink never attenuated the owned key's projection",
    )

    return [
        (root, _ROOT_USER_ID, True, _ROOT_OWNER_ID, ["*"], _ROOT_OWNER_ID, "human"),
        (owner, owner_id, False, None, [SCOPE], owner_id, "human"),
        (owned, owned_id, False, owner_id, [SCOPE], owner_id, "human"),
        (shrunk, shrunk_id, False, svc_id, [], svc_id, "service"),
    ]


async def _assert_forward_reverse(
    client: ApiClient,
    expected_uid: str,
    expected_admin: bool,
    expected_owner: str | None,
    expected_scopes: list[str],
    expected_principal: str,
    expected_kind: str,
) -> bool:
    """Prove one identity's projection agrees with the gate, and return whether the reverse
    direction had teeth (a real route absent from its projection)."""
    me = await client.get("/api/auth/me")

    # Envelope sanity against the minting facts, including the owning principal.
    assert me["user_id"] == expected_uid
    assert me["admin"] is expected_admin
    assert me["owner_user_id"] == expected_owner
    assert sorted(me["scopes"]) == sorted(expected_scopes)
    assert me["principal"]["user_id"] == expected_principal
    assert me["principal"]["kind"] == expected_kind

    projected_pairs = {(entry["path"], method) for entry in me["routes"] for method in entry["methods"]}
    projected_paths = {entry["path"] for entry in me["routes"]}
    # The authenticated carve-out reaches ``/api/auth/me`` for every identity, so
    # the projection is never empty — a sentinel before the sampling below.
    assert "/api/auth/me" in projected_paths, f"{expected_uid}: /api/auth/me missing from its own projection"

    # FORWARD (projection ⊆ gate): every concrete projected GET must be ADMITTED by
    # the gate — never 401/403. A 4xx from the handler (e.g. a missing required
    # query param) still means the gate let the request through, which is the claim.
    # The SSE stream is excluded (it would block); templated paths are not concrete.
    sample = sorted(p for (p, m) in projected_pairs if m == "GET" and "{" not in p and "stream" not in p)
    assert sample, f"{expected_uid}: no concrete GET route projected to sample"
    for path in sample:
        response = await client.request_raw("GET", path)
        assert response.status_code not in (401, 403), (
            f"{expected_uid}: gate DENIED a PROJECTED route GET {path} ({response.status_code})"
        )

    # REVERSE: a real GET route ABSENT from the projection must be gate-denied (403);
    # otherwise the projection would be hiding a door the caller can actually reach.
    absent = [path for path in _KNOWN_REAL_GET if path not in projected_paths]
    for path in absent:
        response = await client.request_raw("GET", path)
        assert response.status_code == 403, (
            f"{expected_uid}: {path} is absent from the projection but the gate returned "
            f"{response.status_code}, not 403"
        )
    return bool(absent)


async def test_projection_agrees_with_gate_across_identity_matrix(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    reverse_had_teeth = False
    for row in await _matrix_rows(owned_keys_stack, uniq):
        reverse_had_teeth = await _assert_forward_reverse(*row) or reverse_had_teeth

    # The scope-stripped identity guarantees the reverse direction was non-vacuous.
    assert reverse_had_teeth, "no identity had a real route absent from its projection — reverse never exercised"
