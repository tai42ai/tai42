"""M20 F1 — the fenced surfaces: ``fenced``/``secret`` routes are NEVER grantable.

The route action-class is the load-bearing admin-only fence: a ``fenced`` mutation or a
``secret`` bulk-read is denied to any non-admin no matter what per-tag level a role
grants (``grant_map_admits`` hard-denies ``fenced``/``secret`` before any level is
consulted). This proves the intersection holds even under the BROADEST grant map, that
the save-time validator refuses an un-grantable tag, and that the base-tier ceiling caps
a write grant (defense in depth).

AS-BUILT reconciliation (verified against tai-skeleton @ the committed RBAC): the
``secret`` admin-only bulk-reads are ``GET /api/auth/roles``,
``GET /api/auth/roles/{name}/versions``,
``GET /api/auth/api-keys/{user_id}/policy/versions`` — the roles/policy version-history
reads that expose raw jq — PLUS ``GET /api/config/env`` and
``GET /api/config/settings-schema``, the env store and settings-schema reads that expose
raw deployment secrets. All five are ``secret``: HARD-denied to any non-admin no matter
the grant map, never opened by a per-tag level. ``GET /api/config/mode`` /
``GET /api/hooks`` (and the other per-tag GETs) stay ``read`` (grantable). The mounted
``fenced`` mutations on the accounts profile are ``POST /api/run-tool``,
``/api/tools/reload``, ``/api/config/env``,
``/api/config/reload``, ``/api/manifest/replace``, ``/api/fleet/reload-config``;
``/api/marketplace/install`` + ``/api/backup/import`` carry the IDENTICAL ``fenced``
class (same ``grant_map_admits`` hard-fence) but their routers are not mounted on this
profile, so the mechanism is proven by the mounted fenced routes.
"""

from __future__ import annotations

from collections.abc import Callable

from _rbac_support import create_role, create_user_with_role, hook_body

from tai42_e2e.stack import TaiStack

# The grantable feature tags the broadest role holds WRITE on — every product family the
# accounts profile mounts a fenced route under, so a fenced-route denial is provably the
# fence (not a missing grant).
_BROAD_GRANTS = {
    "tools": "write",
    "config": "write",
    "manifest": "write",
    "backend": "write",
    "hooks": "write",
    "presets": "write",
    "access-control": "write",
}

# Mounted ``fenced`` mutations and the grantable tag the broad role holds WRITE on — each
# stays admin-only despite the write grant (the editor base tier ALLOWS these non-/api/auth
# paths, so the ONLY thing denying them is the action-class fence).
_FENCED_MUTATIONS = [
    ("POST", "/api/run-tool", {"tool_name": "e2e_echo", "arguments": {}}),
    ("POST", "/api/tools/reload", None),
    ("POST", "/api/config/env", {"env": {}}),
    ("POST", "/api/config/reload", None),
    ("POST", "/api/manifest/replace", {"manifest": {}}),
    ("POST", "/api/fleet/reload-config", None),
]


async def test_broadest_grant_still_denied_every_fence_but_reaches_grantable_reads(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)

    role_name = uniq("broad")
    await create_role(admin, name=role_name, base_tier="editor", grants=dict(_BROAD_GRANTS))
    broad_id, session_token = await create_user_with_role(stack, admin, uniq, role=role_name)
    broad = stack.api(port=stack.port_b).with_token(session_token)

    # Every mounted fenced mutation stays admin-only despite a WRITE grant on its tag.
    for method, path, body in _FENCED_MUTATIONS:
        denied = await broad.request_raw(method, path, json=body)
        assert denied.status_code == 403, (
            f"the broadest grant must STILL be denied the fenced {method} {path}: {denied.status_code} {denied.text}"
        )

    # The grantable READS under the same tags DO succeed — only the fenced/secret routes
    # stay admin-only. (config:write reaches GET /api/config/mode, a grantable config read,
    # while POST /api/config/env stays fenced and GET /api/config/env stays secret.)
    assert (await broad.request_raw("GET", "/api/tools")).status_code == 200, "tools:write must reach GET /api/tools"
    assert (await broad.request_raw("GET", "/api/config/mode")).status_code == 200, (
        "config:write must reach GET /api/config/mode (a grantable read)"
    )

    # The secret bulk-reads stay admin-only even under the broadest grant map — the three
    # roles/policy version reads AND the two config secret reads (env + settings-schema).
    _SECRET_READS = (
        "/api/auth/roles",
        f"/api/auth/roles/{role_name}/versions",
        f"/api/auth/api-keys/{broad_id}/policy/versions",
        "/api/config/env",
        "/api/config/settings-schema",
    )
    for path in _SECRET_READS:
        denied = await broad.request_raw("GET", path)
        assert denied.status_code == 403, (
            f"the broadest grant must STILL be denied the secret read {path}: {denied.status_code} {denied.text}"
        )

    # Admin is allowed the same secret reads (the fence is admin-only, not universally shut).
    for path in _SECRET_READS:
        assert (await admin.request_raw("GET", path)).status_code == 200, f"admin must reach the secret read {path}"


async def test_save_rejects_a_grant_on_an_ungrantable_tag(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The save-time validator refuses a grant on a tag that is not a grantable feature
    group (a nonexistent tag, or one whose routes are all admin-only fenced/secret) — a
    loud 400, so an un-openable grant can never be persisted as dead access. Every
    product tag carries at least one grantable read, so the un-grantable case is a
    nonexistent tag; the ``secret``-read fence itself is proven un-grantable at runtime
    by ``test_broadest_grant_still_denied_every_fence_...``."""
    admin = accounts_stack.api(port=accounts_stack.port_a)
    rejected = await admin.request_raw(
        "POST",
        "/api/auth/roles",
        json={"name": uniq("bad"), "base_tier": "editor", "grants": {uniq("nope-tag"): "read"}},
    )
    assert rejected.status_code == 400, (
        f"a grant on an un-grantable tag must be rejected at save (400): {rejected.status_code} {rejected.text}"
    )


async def test_viewer_base_ceiling_caps_a_write_grant(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """A role on the ``viewer`` base tier granted ``write`` on a tag is STILL denied that
    tag's writes — the viewer read-only jq ceiling denies the write even though the
    per-tag level would grant it (the base-tier ceiling caps the grant; defense in
    depth). The read still succeeds, so the denial is the ceiling, not a missing grant."""
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)

    role_name = uniq("capped")
    await create_role(admin, name=role_name, base_tier="viewer", grants={"hooks": "write"})
    _uid, session_token = await create_user_with_role(stack, admin, uniq, role=role_name)
    capped = stack.api(port=stack.port_b).with_token(session_token)

    # The read succeeds (a viewer may read hooks) — the sentinel that the holder is live.
    allowed = await capped.request_raw("GET", "/api/hooks")
    assert allowed.status_code == 200, f"a viewer-base holder must read hooks: {allowed.status_code} {allowed.text}"

    # The write is denied by the viewer read-only ceiling despite hooks:write (the denial
    # is the ceiling at authz, before the execution_key is ever bound).
    denied = await capped.request_raw("POST", "/api/hooks", json=hook_body(uniq, execution_key=uniq("exec")))
    assert denied.status_code == 403, (
        f"the viewer ceiling must cap the hooks:write grant: {denied.status_code} {denied.text}"
    )
