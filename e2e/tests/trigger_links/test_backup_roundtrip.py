"""Trigger links join the ``webhooks`` backup section, with revocation winning over
restore.

Four legs on the dedicated stack, each observing the door itself (a fire is a 200
that records, or the uniform 404): a permanent + timed link survive the export →
scoped-wipe → import round trip and fire again with their stored ``tool_kwargs``
(hash-at-rest + the per-link-params-and-backup intersection); a revoked link STAYS dead across a restore of a
pre-revocation backup (the tombstone is durable); the disaster-recovery direction
proves the EXPORTED tombstone gates a later restore of the live record; a no-op
re-import does not self-tombstone; and a restore into a now-verified topic counts
``created`` yet the door still answers the uniform 404 (fire-time verifier enforcement)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

from ._trigger_support import MISS_MESSAGE, mint_link, no_auth, register_record_hook, wait_records


async def _export(admin: ApiClient) -> dict[str, Any]:
    """The raw ``webhooks`` backup document (NOT the ``{data}`` envelope)."""
    resp = await admin.request_raw("POST", "/api/backup/export", json={"sections": ["webhooks"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _import(admin: ApiClient, document: dict[str, Any]) -> dict[str, Any]:
    return await admin.post("/api/backup/import", json={"document": document, "sections": ["webhooks"]})


async def _fire(stack: TaiStack, token: str) -> int:
    resp = await no_auth(stack).request_raw("GET", f"/trigger/{token}")
    if resp.status_code == 404:
        assert resp.json() == {"error": MISS_MESSAGE}
    return resp.status_code


async def test_roundtrip_revives_links_with_their_stored_kwargs(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str, wipe_trigger_state: Callable[[], None]
) -> None:
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("t").replace("_", "-")
    rk_perm = uniq("recp")
    rk_timed = uniq("rect")

    # One recording hook on the topic (empty kwargs); each link carries its own
    # ``key``/``value`` so a fire's side effect identifies which link fired.
    await register_record_hook(admin, topic, name=uniq("hook"), execution_key=exec_key, tool_kwargs={})
    permanent = await mint_link(
        admin, topic, ttl_seconds=None, execution_key=exec_key, tool_kwargs={"key": rk_perm, "value": "permval"}
    )
    timed = await mint_link(
        admin, topic, ttl_seconds=3600, execution_key=exec_key, tool_kwargs={"key": rk_timed, "value": "timedval"}
    )

    document = await _export(admin)
    wipe_trigger_state()
    # De-vacuize the revival: after the wipe the permanent link is genuinely gone.
    assert await _fire(stack, permanent["token"]) == 404, "the wipe must have cleared the link"

    result = await _import(admin, document)
    assert result["ok"], f"the webhooks import failed: {result}"

    # The ORIGINAL URLs fire again (hash-at-rest), each WITH its stored tool_kwargs.
    assert await _fire(stack, permanent["token"]) == 200
    assert wait_records(stack, rk_perm, count=1) == ["permval"]
    assert await _fire(stack, timed["token"]) == 200
    assert wait_records(stack, rk_timed, count=1) == ["timedval"]


async def test_revocation_survives_a_pre_revocation_restore(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    """Export while live → revoke → import that PRE-revocation backup → the link
    STAYS 404. The local tombstone (from the revoke) refuses the imported record."""
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("t").replace("_", "-")
    link = await mint_link(admin, topic, ttl_seconds=None, execution_key=exec_key)

    pre_revocation = await _export(admin)
    await admin.delete(f"/api/hooks/trigger-links/{link['name']}")
    assert await _fire(stack, link["token"]) == 404

    result = await _import(admin, pre_revocation)
    assert result["ok"], f"the import must not be a transport failure: {result}"
    assert await _fire(stack, link["token"]) == 404, "a revoked link must stay dead across a pre-revocation restore"


async def test_disaster_recovery_exported_tombstone_gates_a_later_record_restore(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str, wipe_trigger_state: Callable[[], None]
) -> None:
    """The DR direction: revoke → export (carries the tombstone) → scoped wipe →
    import the POST-revocation export → dead. Then ALSO import the PRE-revocation
    export (which carries the LIVE record) → STILL dead — proving the EXPORTED
    tombstone restored and gates (a dropped tombstones array would pass the first
    assertion vacuously)."""
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("t").replace("_", "-")
    link = await mint_link(admin, topic, ttl_seconds=None, execution_key=exec_key)

    pre_revocation = await _export(admin)
    await admin.delete(f"/api/hooks/trigger-links/{link['name']}")
    post_revocation = await _export(admin)

    wipe_trigger_state()
    # The scoped wipe cleared the local tombstone too, so a fire is now a plain miss.
    assert await _fire(stack, link["token"]) == 404

    # Import the POST-revocation export: the tombstone rides it and restores.
    assert (await _import(admin, post_revocation))["ok"]
    assert await _fire(stack, link["token"]) == 404, "the restored tombstone must keep the link dead"

    # Now import the PRE-revocation export carrying the LIVE record: the restored
    # tombstone refuses it, so the link stays dead — only this second import proves
    # the exported tombstone actually restored and gates.
    assert (await _import(admin, pre_revocation))["ok"]
    assert await _fire(stack, link["token"]) == 404, "the exported tombstone must gate the live-record restore"


async def test_noop_reimport_does_not_self_tombstone(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    """Export → import WITHOUT a wipe → the original URL still fires: a same-hash
    re-import updates in place and never self-tombstones the live record."""
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("t").replace("_", "-")
    rkey = uniq("rec")
    await register_record_hook(admin, topic, name=uniq("hook"), execution_key=exec_key, tool_kwargs={})
    link = await mint_link(
        admin, topic, ttl_seconds=None, execution_key=exec_key, tool_kwargs={"key": rkey, "value": "still-fires"}
    )

    document = await _export(admin)
    result = await _import(admin, document)
    assert result["ok"], f"a no-op re-import must succeed: {result}"

    assert await _fire(stack, link["token"]) == 200
    assert wait_records(stack, rkey, count=1) == ["still-fires"]


async def test_restore_skips_the_create_time_verifier_check(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str, wipe_trigger_state: Callable[[], None]
) -> None:
    """A link minted on an unverified topic that LATER gains a verifier restores as
    ``created`` (restore does NOT re-run the create-time verifier check) — yet the door
    answers the uniform 404 (fire-time enforcement). Both wrong directions guarded."""
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("t").replace("_", "-")
    link = await mint_link(admin, topic, ttl_seconds=None, execution_key=exec_key)

    # Bind a verifier AFTER minting; the binding lives in ``hooks:topic_verifiers``,
    # which the scoped wipe deliberately preserves.
    await admin.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={"verifier": "github", "config": {"secret_env": "E2E_GH_WEBHOOK_SECRET"}},
    )
    document = await _export(admin)
    wipe_trigger_state()

    result = await _import(admin, document)
    assert result["ok"], f"the restore must succeed: {result}"
    assert result["sections"]["webhooks"]["created"] >= 1, (
        f"restore must COUNT the link created (no re-run of the create-time verifier check): {result}"
    )

    # Fire-time enforcement: the now-verified topic answers the uniform 404 at the door.
    assert await _fire(stack, link["token"]) == 404, "a restored link on a verified topic must answer the uniform 404"
