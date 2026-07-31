"""The trigger-link fire + revoke journey on the dedicated redis-hooks stack.

A minted token-bearing PUBLIC URL fires a hook topic, multi-use, unauthenticated:
a scan (GET), a curl POST, and a fire through the SIBLING replica all run the
topic's recording hook with the expr-mapped scan payload; the accepted body echoes
NO topic (the link hides it); the list shows the record + hash prefix but NEVER a
token; revoke is immediate + gone-from-list; and every miss — unknown, revoked,
expired, verifier-bound — answers the SAME uniform 404 body (no oracle)."""

from __future__ import annotations

import time
from collections.abc import Callable

from _trigger_support import MISS_MESSAGE, mint_link, no_auth, record_values, register_record_hook, wait_records

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def test_fire_is_multi_use_across_replicas_and_hides_its_topic(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")

    # The hook maps the scan's ``?x=`` query param into the tool's ``value`` (without
    # an expr the scan payload never reaches the tool); ``key`` is the record channel.
    await register_record_hook(
        admin, topic, name=uniq("hook"), execution_key=exec_key, tool_kwargs={"key": rkey}, expr="{value: .x}"
    )
    link = await mint_link(admin, topic, ttl_seconds=None, execution_key=exec_key)
    token = link["token"]
    public = no_auth(stack)

    # Fire 1 — a scan (GET) on replica A, UNAUTHENTICATED.
    first = await public.request_raw("GET", f"/trigger/{token}?x=one")
    assert first.status_code == 200, first.text
    assert first.json() == {"status": "accepted"}, "the accepted body must carry NO topic (the link hides it)"
    assert wait_records(stack, rkey, count=1) == ["one"]

    # Fire 2 — a curl POST with a JSON body (the same door, multi-use, no burn).
    second = await public.request_raw("POST", f"/trigger/{token}", json={"x": "two"})
    assert second.status_code == 200, second.text
    assert second.json() == {"status": "accepted"}
    assert wait_records(stack, rkey, count=2) == ["one", "two"]

    # Fire 3 — through the SIBLING replica B (minted on A, fired via B): shared redis
    # hooks make the token resolvable fleet-wide.
    third = await no_auth(stack, port=stack.port_b).request_raw("GET", f"/trigger/{token}?x=three")
    assert third.status_code == 200, third.text
    assert wait_records(stack, rkey, count=3) == ["one", "two", "three"]

    # The list shows the record + hash prefix, NEVER a raw token (none is stored).
    listing = await admin.get("/api/hooks/trigger-links")
    row = next(item for item in listing["items"] if item["name"] == link["name"])
    assert row["topic"] == topic
    assert row["token_hash_prefix"], "a listed link must carry its hash prefix"
    assert "token" not in row, "a listed link must never expose a raw token"


async def test_revoke_is_immediate_and_gone_from_list(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    await register_record_hook(
        admin, topic, name=uniq("hook"), execution_key=exec_key, tool_kwargs={"key": rkey}, expr="{value: .x}"
    )
    link = await mint_link(admin, topic, ttl_seconds=None, execution_key=exec_key)
    public = no_auth(stack)

    # Alive first: a fire records.
    assert (await public.request_raw("GET", f"/trigger/{link['token']}?x=live")).status_code == 200
    assert wait_records(stack, rkey, count=1) == ["live"]

    removed = await admin.delete(f"/api/hooks/trigger-links/{link['name']}")
    assert removed == {"removed": True, "name": link["name"]}

    # The next fire is the uniform 404 — byte-identical to an unknown token's miss.
    revoked_miss = await public.request_raw("GET", f"/trigger/{link['token']}")
    unknown_miss = await public.request_raw("GET", f"/trigger/trg-{uniq('nope')}")
    assert revoked_miss.status_code == 404
    assert revoked_miss.json() == {"error": MISS_MESSAGE}
    assert revoked_miss.content == unknown_miss.content

    # The revoked link is gone from the list (the name index DEL'd inside the revoke Lua).
    listing = await admin.get("/api/hooks/trigger-links")
    assert all(item["name"] != link["name"] for item in listing["items"])
    # No further side effect fired.
    assert record_values(stack, rkey) == ["live"]


async def test_all_misses_are_byte_identical(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    """Unknown, revoked, expired, and verifier-bound all answer the SAME 404 bytes —
    the public surface leaks no oracle distinguishing why a fire missed."""
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    public = no_auth(stack)

    # (1) Unknown — a never-minted token; the shared baseline the rest must match.
    unknown = await public.request_raw("GET", f"/trigger/trg-{uniq('nope')}")
    assert unknown.status_code == 404
    assert unknown.json() == {"error": MISS_MESSAGE}

    # (2) Revoked — mint then revoke.
    revoked_link = await mint_link(admin, uniq("t").replace("_", "-"), ttl_seconds=None, execution_key=exec_key)
    await admin.delete(f"/api/hooks/trigger-links/{revoked_link['name']}")
    revoked = await public.request_raw("GET", f"/trigger/{revoked_link['token']}")
    assert revoked.status_code == 404

    # (3) Expired — a short-TTL link waited out (a pure time predicate, no sleep).
    ttl_seconds = 2
    expiring_link = await mint_link(admin, uniq("t").replace("_", "-"), ttl_seconds=ttl_seconds, execution_key=exec_key)
    created = time.monotonic()

    async def past_ttl() -> bool:
        return time.monotonic() >= created + ttl_seconds + 1.0

    await wait_for_async(past_ttl, deadline=20.0, interval=0.25, message="the link TTL window never elapsed")
    expired = await public.request_raw("GET", f"/trigger/{expiring_link['token']}")
    assert expired.status_code == 404

    # (4) Verifier-bound — mint for an unverified topic, then bind a verifier: a topic
    # verified after the link was minted answers the uniform 404 at the door.
    verified_topic = uniq("t").replace("_", "-")
    bound_link = await mint_link(admin, verified_topic, ttl_seconds=None, execution_key=exec_key)
    await admin.put(
        f"/api/hooks/topics/{verified_topic}/verifier",
        json={"verifier": "github", "config": {"secret_env": "E2E_GH_WEBHOOK_SECRET"}},
    )
    verifier_bound = await public.request_raw("GET", f"/trigger/{bound_link['token']}")
    assert verifier_bound.status_code == 404

    # Every miss body is byte-equal to the unknown baseline.
    assert revoked.content == unknown.content
    assert expired.content == unknown.content
    assert verifier_bound.content == unknown.content
