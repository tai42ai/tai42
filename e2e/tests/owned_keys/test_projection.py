"""The caller-capability projection (``GET /api/auth/me``) agrees with the enforced
gate on the deployed stack: for a matrix of identity shapes (root ``*``, an
unrestricted owner, its restricted owned key, and an owned key whose owner's scopes
were shrunk out from under it) the projection NEVER advertises a door the gate denies,
and every real route it omits is one the gate really denies. Sampled — the exhaustive
property is the unit suite's; this proves the running deployment agrees. The envelope's
identity facts are checked against the minting facts."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._owned_support import SCOPE, mint_owned, mint_owner

# The root key the auth bootstrap seeds (``seed_bootstrap_key`` → ``e2e-root``, ``*``).
_ROOT_USER_ID = "e2e-root"

# Real GET routes every ``e2e-all`` holder can reach; a scope-stripped identity cannot,
# so they must be ABSENT from its projection AND gate-denied (the reverse direction).
_KNOWN_REAL_GET = ["/api/tools", "/api/notifications", "/api/presets", "/api/schedules", "/api/extensions"]


async def test_projection_agrees_with_gate_across_identity_matrix(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    owned_id, owned_raw = await mint_owned(owner, uniq)

    # The 4th shape: an owned key whose OWNER's scopes are shrunk out from under it, so
    # its effective (owner-attenuated) scope set empties by PROPAGATION — the real
    # shrink path, not a scope-stripped mint. Minted with e2e-all under a second owner,
    # then that owner is shrunk to a fresh unrelated scope (registered first, as a scope
    # must exist before it can be assigned); the owned key keeps e2e-all but its owner no
    # longer holds it, so the request-time intersection — and thus the projection —
    # collapses to only the authenticated carve-out.
    shrunk_owner_id, shrunk_owner_raw = await mint_owner(root, uniq)
    shrunk_id, shrunk_raw = await mint_owned(root.with_token(shrunk_owner_raw), uniq)
    shrunk = root.with_token(shrunk_raw)
    # Sentinel: before the shrink the owned key's projection still carries e2e-all.
    assert sorted((await shrunk.get("/api/auth/me"))["scopes"]) == [SCOPE]
    other_scope = uniq("scope")
    await root.post("/api/auth/scopes", json={"scope_id": other_scope, "url": f"/e2e/{other_scope}"})
    await root.put(f"/api/auth/api-keys/{shrunk_owner_id}", json={"scopes": [other_scope]})

    async def shrunk_projection_empty() -> bool:
        return (await shrunk.get("/api/auth/me"))["scopes"] == []

    await wait_for_async(
        shrunk_projection_empty,
        deadline=5.0,
        message="owner scope shrink never attenuated the owned key's projection",
    )

    # (client, expected user_id, expected admin, expected owner_user_id, expected scopes)
    matrix = [
        (root, _ROOT_USER_ID, True, None, ["*"]),
        (owner, owner_id, False, None, [SCOPE]),
        (root.with_token(owned_raw), owned_id, False, owner_id, [SCOPE]),
        (shrunk, shrunk_id, False, shrunk_owner_id, []),
    ]

    reverse_had_teeth = False
    for client, expected_uid, expected_admin, expected_owner, expected_scopes in matrix:
        me = await client.get("/api/auth/me")

        # Envelope sanity against the minting facts.
        assert me["user_id"] == expected_uid
        assert me["admin"] is expected_admin
        assert me["owner_user_id"] == expected_owner
        assert sorted(me["scopes"]) == sorted(expected_scopes)

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
        reverse_had_teeth = reverse_had_teeth or bool(absent)

    # The scope-stripped identity guarantees the reverse direction was non-vacuous.
    assert reverse_had_teeth, "no identity had a real route absent from its projection — reverse never exercised"
