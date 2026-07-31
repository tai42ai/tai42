"""Create-time validation, the verifier fence at both ends, and the authorization
boundary for the trigger-link CRUD surface.

The CRUD lives on the HOOKS feature: grantable ``read``/``write`` under the ``hooks``
tag. A hooks-WRITE role manages links, a hooks-READ role lists them, a hooks-NONE
role reaches none, and no credential is denied — while the resolver door stays
public (proven in the fire suite)."""

from __future__ import annotations

from collections.abc import Callable

from _rbac_support import (  # pyright: ignore[reportMissingImports]  (resolved via the conftest path insertion)
    create_role,
    create_user_with_role,
    mint_owned_key,
)
from _trigger_support import MISS_MESSAGE, mint_link, no_auth

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# A verifier config naming an env var that is only dereferenced at signature-VERIFY
# time; the BIND validates the verifier NAME against the registry, so no secret is
# needed and no fenced leg here delivers a signed payload.
_VERIFIER_BODY = {"verifier": "github", "config": {"secret_env": "E2E_GH_WEBHOOK_SECRET"}}


async def test_create_validation_rules(trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str) -> None:
    admin = trigger_stack.api(port=trigger_stack.port_a)
    topic = uniq("t").replace("_", "-")

    # ttl_seconds is REQUIRED and must be a positive int or null; absent / 0 / negative
    # each fail as a loud 400 (the creator's explicit expiry choice, no default). A valid
    # execution_key is supplied so the 400 is the ttl rule, not a missing-field short-circuit.
    absent = await admin.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": topic, "execution_key": exec_key}
    )
    assert absent.status_code == 400, absent.text
    zero = await admin.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": topic, "ttl_seconds": 0, "execution_key": exec_key}
    )
    assert zero.status_code == 400, zero.text
    negative = await admin.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": topic, "ttl_seconds": -5, "execution_key": exec_key}
    )
    assert negative.status_code == 400, negative.text

    # A non-dict tool_kwargs is rejected (it is stored and merged as a JSON object).
    bad_kwargs = await admin.request_raw(
        "POST",
        "/api/hooks/trigger-links",
        json={"topic": topic, "ttl_seconds": None, "tool_kwargs": [1, 2], "execution_key": exec_key},
    )
    assert bad_kwargs.status_code == 400, bad_kwargs.text

    # A duplicate explicit name is a loud 409.
    name = uniq("dup")
    first = await admin.request_raw(
        "POST",
        "/api/hooks/trigger-links",
        json={"topic": topic, "name": name, "ttl_seconds": None, "execution_key": exec_key},
    )
    assert first.status_code == 200, first.text
    dup = await admin.request_raw(
        "POST",
        "/api/hooks/trigger-links",
        json={"topic": topic, "name": name, "ttl_seconds": None, "execution_key": exec_key},
    )
    assert dup.status_code == 409, dup.text


async def test_verifier_bound_topic_refuses_links_both_ends(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    """A verifier-bound topic refuses trigger links at BOTH ends: CREATE for a
    verified topic is a 400 naming the conflict; a topic verified AFTER a link was
    minted answers the uniform 404 at the door."""
    admin = trigger_stack.api(port=trigger_stack.port_a)

    # CREATE end — bind first, then create is refused with a 400.
    verified = uniq("t").replace("_", "-")
    await admin.put(f"/api/hooks/topics/{verified}/verifier", json=_VERIFIER_BODY)
    refused = await admin.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": verified, "ttl_seconds": None, "execution_key": exec_key}
    )
    assert refused.status_code == 400, refused.text

    # FIRE end — mint on an unverified topic, then bind a verifier: the door refuses.
    late = uniq("t").replace("_", "-")
    link = await mint_link(admin, late, ttl_seconds=None, execution_key=exec_key)
    await admin.put(f"/api/hooks/topics/{late}/verifier", json=_VERIFIER_BODY)
    fired = await no_auth(trigger_stack).request_raw("GET", f"/trigger/{link['token']}")
    assert fired.status_code == 404
    assert fired.json() == {"error": MISS_MESSAGE}


async def test_crud_requires_authentication(trigger_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """No credential is denied on every CRUD route (they resolve to a protected
    scope); the public resolver door is the only unauthenticated surface."""
    public = no_auth(trigger_stack)
    created = await public.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": uniq("t").replace("_", "-"), "ttl_seconds": None}
    )
    assert created.status_code == 401, created.text
    listed = await public.request_raw("GET", "/api/hooks/trigger-links")
    assert listed.status_code == 401, listed.text
    deleted = await public.request_raw("DELETE", f"/api/hooks/trigger-links/{uniq('x')}")
    assert deleted.status_code == 401, deleted.text


async def _assert_write_reach(client: ApiClient, uniq: Callable[[str], str], *, execution_key: str) -> None:
    """hooks:write reaches create, list, AND delete."""
    created = await client.request_raw(
        "POST",
        "/api/hooks/trigger-links",
        json={"topic": uniq("t").replace("_", "-"), "ttl_seconds": None, "execution_key": execution_key},
    )
    assert created.status_code == 200, f"hooks:write must reach create: {created.status_code} {created.text}"
    name = created.json()["data"]["name"]
    listed = await client.request_raw("GET", "/api/hooks/trigger-links")
    assert listed.status_code == 200, f"hooks:write must reach list: {listed.status_code} {listed.text}"
    deleted = await client.request_raw("DELETE", f"/api/hooks/trigger-links/{name}")
    assert deleted.status_code == 200, f"hooks:write must reach delete: {deleted.status_code} {deleted.text}"


async def _assert_read_reach(client: ApiClient, uniq: Callable[[str], str]) -> None:
    """hooks:read reaches the list but is DENIED the mutations."""
    listed = await client.request_raw("GET", "/api/hooks/trigger-links")
    assert listed.status_code == 200, f"hooks:read must reach list: {listed.status_code} {listed.text}"
    created = await client.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": uniq("t").replace("_", "-"), "ttl_seconds": None}
    )
    assert created.status_code == 403, f"hooks:read must be denied create: {created.status_code} {created.text}"
    deleted = await client.request_raw("DELETE", f"/api/hooks/trigger-links/{uniq('x')}")
    assert deleted.status_code == 403, f"hooks:read must be denied delete: {deleted.status_code} {deleted.text}"


async def _assert_none_reach(client: ApiClient, uniq: Callable[[str], str]) -> None:
    """hooks-none reaches NONE of the three routes."""
    listed = await client.request_raw("GET", "/api/hooks/trigger-links")
    assert listed.status_code == 403, f"hooks-none must be denied list: {listed.status_code} {listed.text}"
    created = await client.request_raw(
        "POST", "/api/hooks/trigger-links", json={"topic": uniq("t").replace("_", "-"), "ttl_seconds": None}
    )
    assert created.status_code == 403, f"hooks-none must be denied create: {created.status_code} {created.text}"
    deleted = await client.request_raw("DELETE", f"/api/hooks/trigger-links/{uniq('x')}")
    assert deleted.status_code == 403, f"hooks-none must be denied delete: {deleted.status_code} {deleted.text}"


async def test_per_tag_grant_matrix(trigger_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The CRUD is grantable under the ``hooks`` tag: a hooks-write role manages
    links, a hooks-read role lists them, a hooks-none role reaches none. Each role is
    assigned to a user session and enforced on replica B (the admin mutated on A)."""
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)

    write_role = uniq("twrite")
    read_role = uniq("tread")
    none_role = uniq("tnone")
    await create_role(admin, name=write_role, base_tier="editor", grants={"hooks": "write"})
    await create_role(admin, name=read_role, base_tier="editor", grants={"hooks": "read"})
    await create_role(admin, name=none_role, base_tier="editor", grants={})

    write_id, write_token = await create_user_with_role(stack, admin, uniq, role=write_role)
    _read_id, read_token = await create_user_with_role(stack, admin, uniq, role=read_role)
    _none_id, none_token = await create_user_with_role(stack, admin, uniq, role=none_role)

    # The write holder binds a key it owns as the create's execution_key (its own session
    # is not a mintable key); the read/none creates are denied at authz before any bind.
    _write_key_raw, write_exec = await mint_owned_key(admin, uniq, owner_user_id=write_id)

    await _assert_write_reach(stack.api(port=stack.port_b).with_token(write_token), uniq, execution_key=write_exec)
    await _assert_read_reach(stack.api(port=stack.port_b).with_token(read_token), uniq)
    await _assert_none_reach(stack.api(port=stack.port_b).with_token(none_token), uniq)
