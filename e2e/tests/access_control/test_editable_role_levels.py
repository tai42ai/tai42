"""M20 F1 — the editable-role LEVEL journey: create → assign → enforce at the grant
level, LIVE role edits, and keys-inherit-owner.

A custom role is a per-tag ACCESS-LEVEL map (``{tag: none|read|write}``) over a base
tier. Enforcement INTERSECTS the base-tier jq ceiling with the per-tag level and the
route action-class fence (fail-closed AND). This drives the real skeleton over HTTP on
the REPLICAS accounts stack: the admin mutates on replica A, a holder is enforced on
replica B, and a role edit's policy-version bump propagates cross-worker so the SAME
holder's reach changes on the next request — no reassignment.

The grant map under test is ``{hooks: write, presets: read}``:
- ``hooks: write`` reaches the hooks GETs AND its writes (write satisfies read+write);
- ``presets: read`` reaches the presets GETs but is DENIED the presets writes;
- ``config`` is ungranted (absent → ``none``) so its reads are denied.
Both a directly-assigned user SESSION and an owner-attenuated KEY are asserted, so the
key-inherits-owner (D9) leg rides the same grant map the session does.
"""

from __future__ import annotations

from collections.abc import Callable

from _rbac_support import create_role, create_user_with_role, hook_body, mint_owned_key, update_role_grants

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async


async def _assert_reach_under_reviewer(
    client: ApiClient, uniq: Callable[[str], str], *, execution_key: str, label: str
) -> None:
    """The reviewer grant map ``{hooks: write, presets: read}`` enforced on one holder.

    ``hooks: write`` reaches the hooks reads AND writes; ``presets: read`` reaches the
    presets reads but not its writes; the ungranted ``config`` tag denies its reads."""
    # hooks: write — reaches the tag's GETs (write satisfies read) AND its writes.
    hooks_get = await client.request_raw("GET", "/api/hooks")
    assert hooks_get.status_code == 200, (
        f"{label}: hooks:write must reach GET /api/hooks: {hooks_get.status_code} {hooks_get.text}"
    )
    hooks_post = await client.request_raw("POST", "/api/hooks", json=hook_body(uniq, execution_key=execution_key))
    assert hooks_post.status_code == 200, (
        f"{label}: hooks:write must reach POST /api/hooks: {hooks_post.status_code} {hooks_post.text}"
    )

    # New-route action-inherit: a hooks route the role never named individually is
    # reachable at the TAG's level — the grant is per-tag, so every route under the tag
    # inherits it (no per-route grant, no role edit).
    verifiers = await client.request_raw("GET", "/api/hooks/verifiers")
    assert verifiers.status_code == 200, (
        f"{label}: a granted tag's OTHER route must inherit the level: {verifiers.status_code} {verifiers.text}"
    )

    # presets: read — reaches the tag's GETs but is DENIED its writes (read < write).
    presets_get = await client.request_raw("GET", "/api/presets")
    assert presets_get.status_code == 200, (
        f"{label}: presets:read must reach GET /api/presets: {presets_get.status_code} {presets_get.text}"
    )
    presets_post = await client.request_raw("POST", "/api/presets", json={"name": uniq("p"), "tool": "e2e_echo"})
    assert presets_post.status_code == 403, (
        f"{label}: presets:read must DENY POST /api/presets: {presets_post.status_code} {presets_post.text}"
    )

    # config: none (absent tag) — its reads are denied (default-deny per-tag posture).
    config_get = await client.request_raw("GET", "/api/config/mode")
    assert config_get.status_code == 403, (
        f"{label}: an ungranted tag must deny its reads: {config_get.status_code} {config_get.text}"
    )


async def test_editable_role_level_journey_live_and_keys_inherit(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # the seeded root sk- key (unconditional "*" admin)

    # -- Create the custom role and assign it two ways: a user SESSION + an owned KEY --
    role_name = uniq("reviewer")
    await create_role(admin, name=role_name, base_tier="editor", grants={"hooks": "write", "presets": "read"})
    reviewer_id, session_token = await create_user_with_role(stack, admin, uniq, role=role_name)
    key_raw, owned_key_id = await mint_owned_key(admin, uniq, owner_user_id=reviewer_id)

    # Enforce the holders on replica B (the admin mutated on replica A).
    session = stack.api(port=stack.port_b).with_token(session_token)
    key = stack.api(port=stack.port_b).with_token(key_raw)

    # Both holders bind the owned key as the hook's execution_key: the session OWNS it
    # (owner == reviewer), and the key itself IS that identity. The SAME grant map is
    # enforced through the directly-assigned session AND the owner-attenuated key.
    await _assert_reach_under_reviewer(session, uniq, execution_key=owned_key_id, label="reviewer session")
    await _assert_reach_under_reviewer(key, uniq, execution_key=owned_key_id, label="reviewer-owned key")

    # -- LIVE role edit: widen the map (add config: read) WITHOUT reassigning ----------
    # The version bump busts the grant cache, so the SAME session AND the SAME key gain
    # config reads on their next request (assert LIVE directly — no COPY, no reassign).
    await update_role_grants(admin, role_name, {"hooks": "write", "presets": "read", "config": "read"})

    async def config_read_now_allowed(client: ApiClient) -> bool:
        resp = await client.request_raw("GET", "/api/config/mode")
        return resp.status_code == 200

    await wait_for_async(
        lambda: config_read_now_allowed(session),
        deadline=10.0,
        message="the reviewer session never gained config reads after the LIVE role edit",
    )
    await wait_for_async(
        lambda: config_read_now_allowed(key),
        deadline=10.0,
        message="the reviewer-owned key never gained config reads after the LIVE role edit (keys-inherit-owner)",
    )

    # The unedited grants are untouched by the edit: presets stays read-only (writes
    # still denied), so the edit widened exactly one tag and nothing else drifted.
    still_denied = await session.request_raw("POST", "/api/presets", json={"name": uniq("p"), "tool": "e2e_echo"})
    assert still_denied.status_code == 403, (
        f"the LIVE edit must not have opened presets writes: {still_denied.status_code} {still_denied.text}"
    )


async def test_non_admin_denied_every_roles_mutation_admin_unaffected(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A non-admin role holder is denied EVERY ``/api/auth/roles`` route (the roles
    listing is a ``secret`` read; each mutation is a ``fenced`` admin-only route), while
    the admin policy is unaffected by the role pointer — admin still reaches everything.
    """
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)

    role_name = uniq("reviewer")
    await create_role(admin, name=role_name, base_tier="editor", grants={"hooks": "write"})
    reviewer_id, session_token = await create_user_with_role(stack, admin, uniq, role=role_name)
    reviewer = stack.api(port=stack.port_b).with_token(session_token)

    # Every /api/auth/roles route is admin-only — no per-tag level opens the fence, and
    # the editor base-tier jq fences the /api/auth control plane besides.
    listing = await reviewer.request_raw("GET", "/api/auth/roles")
    assert listing.status_code == 403, (
        f"the roles listing (secret) must deny a non-admin: {listing.status_code} {listing.text}"
    )
    creating = await reviewer.request_raw(
        "POST", "/api/auth/roles", json={"name": uniq("x"), "base_tier": "viewer", "grants": {}}
    )
    assert creating.status_code == 403, (
        f"role create (fenced) must deny a non-admin: {creating.status_code} {creating.text}"
    )
    editing = await reviewer.request_raw("PUT", f"/api/auth/roles/{role_name}", json={"grants": {}})
    assert editing.status_code == 403, f"role edit (fenced) must deny a non-admin: {editing.status_code} {editing.text}"
    deleting = await reviewer.request_raw("DELETE", f"/api/auth/roles/{role_name}")
    assert deleting.status_code == 403, (
        f"role delete (fenced) must deny a non-admin: {deleting.status_code} {deleting.text}"
    )

    # The admin policy is unaffected by any role pointer: the seeded root "*" admin still
    # reaches the secret roles listing, a normal read, and a write. (Its policy carries no
    # role pointer and a null condition_id — the admin discriminator — asserted at the
    # skeleton unit level; here the observable is that admin reaches everything a
    # non-admin was just denied.)
    assert (await admin.request_raw("GET", "/api/auth/roles")).status_code == 200
    assert (await admin.request_raw("GET", "/api/config/mode")).status_code == 200
    # Admin binds any existing key as the hook's execution_key; mint one to bind.
    _exec_raw, exec_key = await mint_owned_key(admin, uniq, owner_user_id=reviewer_id)
    admin_hook = await admin.request_raw("POST", "/api/hooks", json=hook_body(uniq, execution_key=exec_key))
    assert admin_hook.status_code == 200, admin_hook.text
