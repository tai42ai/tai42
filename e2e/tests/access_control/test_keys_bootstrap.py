"""C-access-control — the first-key bootstrap door, end to end.

A fresh install with access control ON and the redis key provider has no seeded key.
This drives the composed path the fix restores: the public ``/api/keys/bootstrap`` door
mints the first admin key behind the boot-time token, and that minted key then reaches
an authenticated route. Born red on the unfixed platform — with no bootstrap door, a
keyless gate-on deployment cannot mint its first credential at all.
"""

from __future__ import annotations

import asyncio

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# Must equal ``manifests._KEYS_BOOTSTRAP_TOKEN`` (the value the stack env pins).
_TOKEN = "e2e-keys-bootstrap-token"


def _public(stack: TaiStack) -> ApiClient:
    """A client for the stack carrying NO credential — the fresh install has no key."""
    return ApiClient(f"http://{stack.host}:{stack.port_a}")


async def test_bootstrap_mints_the_first_admin_key_and_it_authenticates(
    keys_bootstrap_stack: TaiStack,
) -> None:
    stack = keys_bootstrap_stack
    public = _public(stack)

    # An authed route is unreachable on the fresh, keyless deployment.
    pre = await public.request_raw("GET", "/api/auth/me")
    assert pre.status_code == 401, f"a keyless authed route must 401: {pre.status_code} {pre.text}"

    # A wrong token is a generic 403 — no oracle for the initialized state.
    bad = await public.request_raw(
        "POST",
        "/api/keys/bootstrap",
        json={"user_id": "root", "description": "root key", "bootstrap_token": "wrong-token"},
    )
    assert bad.status_code == 403, f"a wrong bootstrap token must 403: {bad.status_code} {bad.text}"

    # Two correct-token bootstraps in parallel with DIFFERENT user_ids race the mint mutex:
    # exactly one mints (200), the other is turned away 409 — never two admin keys.
    res_a, res_b = await asyncio.gather(
        public.request_raw(
            "POST",
            "/api/keys/bootstrap",
            json={"user_id": "root", "description": "root key", "bootstrap_token": _TOKEN},
        ),
        public.request_raw(
            "POST", "/api/keys/bootstrap", json={"user_id": "root-2", "description": "other", "bootstrap_token": _TOKEN}
        ),
    )
    statuses = sorted([res_a.status_code, res_b.status_code])
    assert statuses == [200, 409], f"exactly one bootstrap must win: {statuses} ({res_a.text} | {res_b.text})"
    winner = res_a if res_a.status_code == 200 else res_b
    minted = winner.json()["data"]
    key = minted["token"]
    assert key.startswith("sk-"), minted

    # The minted key authenticates and is a FULL admin — the composed proof the fix exists
    # for: the door's key reaches the authed identity route and reports the admin "*".
    admin = ApiClient(f"http://{stack.host}:{stack.port_a}", auth_token=key)
    me = await admin.get("/api/auth/me")
    assert me["admin"] is True, me
    assert "*" in me["scopes"], me

    # One-shot: a second bootstrap with the same token is refused now a key exists.
    second = await public.request_raw(
        "POST",
        "/api/keys/bootstrap",
        json={"user_id": "second", "description": "another", "bootstrap_token": _TOKEN},
    )
    assert second.status_code == 409, f"a second bootstrap must 409: {second.status_code} {second.text}"
