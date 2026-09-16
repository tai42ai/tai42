"""Per-identity isolation A/B negatives on the tool-runs seam. Identity A starts a run
(sentinel), identity B does not see it (list absence + direct-id 403), and the
unrestricted operator sees everything. A structural pin goes beyond simple exclusion: A's
own run index stays COMPLETE under a shared-window flood that evicts the run from the
operator's recent window. A second pin proves isolation follows the key's OWN id, not its
owner: two sibling keys under ONE service owner are foreign identities to each other."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.stack import TaiStack

from ._owned_support import create_service_owner, mint_key_for, provision_operator, two_service_identities


def _ids(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["run_id"] for entry in entries]


async def test_tool_run_isolation_and_completeness(owned_keys_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    _owned_a, owned_a_raw, _owned_b, owned_b_raw = await two_service_identities(owned_keys_stack, uniq)
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    # The unrestricted operator: an admin session is a top-level principal with no owner
    # claim, so the isolation gates give it the full cross-identity view. The seeded admin
    # root key is unrestricted too — admin is never confined to a slice, owned or not.
    operator = root.with_token(await provision_operator(owned_keys_stack, root, uniq))
    owned_a = root.with_token(owned_a_raw)
    owned_b = root.with_token(owned_b_raw)

    submitted = await owned_a.post(
        "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "a"}}, expect=202
    )
    run_id = submitted["run_id"]

    # A sees its own run — by id and in its list (the sentinel).
    got = await owned_a.get(f"/api/tool-runs/{run_id}")
    assert got["run_id"] == run_id
    assert run_id in _ids(await owned_a.get("/api/tool-runs?tool_name=e2e_echo"))

    # B cannot: a NAMED run of another identity is a 403 (never a lying 404), and it is
    # absent from B's own list.
    denied = await owned_b.request_raw("GET", f"/api/tool-runs/{run_id}")
    assert denied.status_code == 403, denied.text
    assert run_id not in _ids(await owned_b.get("/api/tool-runs?tool_name=e2e_echo"))

    # The unrestricted operator sees everything.
    assert (await operator.get(f"/api/tool-runs/{run_id}"))["run_id"] == run_id
    assert run_id in _ids(await operator.get("/api/tool-runs?tool_name=e2e_echo"))

    # Completeness pin: flood the shared window (recent limit is 3 on this stack) with
    # other-identity runs. A's own list STILL carries its run (per-identity index),
    # while the shared window has evicted it (the sentinel the flood actually overflowed).
    for _ in range(5):
        await operator.post(
            "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "flood"}}, expect=202
        )
    assert run_id in _ids(await owned_a.get("/api/tool-runs?tool_name=e2e_echo"))
    assert run_id not in _ids(await operator.get("/api/tool-runs?tool_name=e2e_echo"))


async def test_key_own_not_owner_tool_runs_two_siblings_under_one_owner(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The key-own-vs-owner pin for the TOOL-RUNS seam. TWO keys minted under the SAME
    service owner share an owner claim but carry DIFFERENT own ids, so under the key-keyed
    model each is its OWN island: a run's ownership follows the starter's OWN id, never
    the shared owner. Sibling-1 owns its run; sibling-2 — same owner — is a foreign
    identity to it (GET-by-id 403, absent from its list). The owner-keyed model would
    FAIL this: a shared owner would let sibling-2 read sibling-1's run."""
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    owner_id = await create_service_owner(root, uniq)
    # Two siblings under the one owner: same owner claim, distinct own ids.
    owned_1_id, owned_1_raw = await mint_key_for(root, uniq, owner_id)
    owned_2_id, owned_2_raw = await mint_key_for(root, uniq, owner_id)
    assert owned_1_id != owner_id
    assert owned_2_id != owner_id
    # Distinct own ids are what makes them siblings rather than one identity.
    assert owned_1_id != owned_2_id
    owned_1 = root.with_token(owned_1_raw)
    owned_2 = root.with_token(owned_2_raw)

    # Sibling-1 starts (and so owns) a background run.
    submitted = await owned_1.post(
        "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "s1"}}, expect=202
    )
    run_id = submitted["run_id"]

    # Sibling-1 sees its own run — by id and in its list (the sentinel).
    got = await owned_1.get(f"/api/tool-runs/{run_id}")
    assert got["run_id"] == run_id
    assert run_id in _ids(await owned_1.get("/api/tool-runs?tool_name=e2e_echo"))

    # The SIBLING under the SAME owner does NOT: a NAMED run of another identity is a
    # loud 403 (never a lying 404), and it is absent from the sibling's own list —
    # isolation follows the own id, so a shared owner does NOT share a run slice.
    denied = await owned_2.request_raw("GET", f"/api/tool-runs/{run_id}")
    assert denied.status_code == 403, denied.text
    assert "belongs to another identity" in denied.text
    assert run_id not in _ids(await owned_2.get("/api/tool-runs?tool_name=e2e_echo"))
