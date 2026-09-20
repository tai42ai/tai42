"""C-access-control — the setup door, end to end.

A fresh install with access control ON and the redis key provider has no principal. The
public ``POST /api/setup`` door initializes the deployment once behind the boot-time setup
token: it creates the owner principal, mints the owner's first key, and returns the raw key
once. The door is one-shot (409 after the owner exists), per-IP throttled (the backoff is
consulted before the token compare), and — with ``TAI_SETUP_OPEN`` — ungated for local dev.

Each scenario boots its OWN fresh deployment: the door mutates global state (the owner
principal, the per-IP throttle counters) that a shared stack would leak to the next spec,
and the split matrix gives no ordering guarantee.
"""

from __future__ import annotations

import asyncio

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import _SETUP_TOKEN, build_setup_stack
from tai42_e2e.stack import TaiStack

# The setup throttle arms a backoff once wrong-token attempts pass the threshold; the
# skeleton's ``setup_throttle_threshold`` default is 5, so the 6th wrong attempt from one IP
# arms the lock. Kept as a local constant the flood loop reads.
_THROTTLE_THRESHOLD = 5


def _public(stack: TaiStack) -> ApiClient:
    """A client for the stack carrying NO credential — the fresh install has no key."""
    return ApiClient(f"http://{stack.host}:{stack.port_a}")


def _setup_body(owner_id: str, *, token: str = _SETUP_TOKEN) -> dict:
    return {"setup_token": token, "owner_user_id": owner_id, "owner_display_name": f"{owner_id} display"}


async def test_fresh_deployment_reports_needs_setup(fresh_setup_stack: TaiStack) -> None:
    stack = fresh_setup_stack
    public = _public(stack)

    # An authed route is unreachable on the fresh, principal-less deployment.
    pre = await public.request_raw("GET", "/api/auth/me")
    assert pre.status_code == 401, f"a credential-less authed route must 401: {pre.status_code} {pre.text}"

    # /api/login/methods is PUBLIC (the always-public /api/login prefix) and reports the one
    # platform fact "no principal exists". This keys-only stack has no login-attaching
    # provider, so the setup door can attach no interactive login.
    methods = await public.get("/api/login/methods")
    assert methods["needs_setup"] is True, methods
    assert methods["setup_login"] is None, methods


async def test_wrong_token_throttles_before_the_compare(fresh_setup_stack: TaiStack) -> None:
    stack = fresh_setup_stack
    public = _public(stack)

    # A wrong token is a generic 403 — no oracle for the initialized state. Flood past the
    # threshold from one IP so the backoff lock arms.
    for _ in range(_THROTTLE_THRESHOLD + 1):
        bad = await public.request_raw("POST", "/api/setup", json=_setup_body("owner", token="wrong-token"))
        assert bad.status_code == 403, f"a wrong setup token must 403: {bad.status_code} {bad.text}"

    # The backoff is consulted BEFORE the token compare, so even the CORRECT token is turned
    # away 403 while throttled — proof the throttle gates ahead of the compare (an
    # un-throttled correct token would have initialized the deployment, a 200).
    throttled = await public.request_raw("POST", "/api/setup", json=_setup_body("owner"))
    assert throttled.status_code == 403, f"a throttled correct token must 403: {throttled.status_code} {throttled.text}"


async def test_setup_initializes_once_and_is_idempotent(fresh_setup_stack: TaiStack) -> None:
    stack = fresh_setup_stack
    public = _public(stack)

    # Two correct-token setups in parallel with DIFFERENT owners race the mint mutex: exactly
    # one initializes (200), the other is turned away 409 — never two owners.
    res_a, res_b = await asyncio.gather(
        public.request_raw("POST", "/api/setup", json=_setup_body("owner-a")),
        public.request_raw("POST", "/api/setup", json=_setup_body("owner-b")),
    )
    statuses = sorted([res_a.status_code, res_b.status_code])
    assert statuses == [200, 409], f"exactly one setup must win: {statuses} ({res_a.text} | {res_b.text})"
    winner = res_a if res_a.status_code == 200 else res_b
    initialized = winner.json()["data"]
    owner_id = initialized["owner_user_id"]
    key = initialized["api_key"]
    assert key.startswith("sk-"), initialized
    # A keys-only deployment attaches no login: the door reports neither an attached login
    # nor an invite.
    assert initialized["login_attached"] is False, initialized
    assert initialized["invite_token"] is None, initialized

    # The minted key authenticates and is a FULL admin, and its projection names the owner
    # principal it belongs to.
    admin = ApiClient(f"http://{stack.host}:{stack.port_a}", auth_token=key)
    me = await admin.get("/api/auth/me")
    assert me["admin"] is True, me
    assert "*" in me["scopes"], me
    assert me["principal"] == {"user_id": owner_id, "kind": "human", "display_name": f"{owner_id} display"}, me

    # The owner principal is the NULL-creator row the setup door mints.
    principals = await admin.get("/api/auth/principals")
    owner_row = next((p for p in principals if p["user_id"] == owner_id), None)
    assert owner_row is not None, principals
    assert owner_row["created_by"] is None, owner_row
    assert owner_row["kind"] == "human", owner_row

    # The owner's key shows its principal in the tokens-payload listing.
    tokens = await admin.get("/api/auth/tokens-payload")
    key_row = next((t for t in tokens if t["user_id"] == initialized["key_user_id"]), None)
    assert key_row is not None, tokens
    assert key_row["principal"]["user_id"] == owner_id, key_row

    # One-shot: a sequential second setup is refused once the owner exists, and the platform
    # reports needs_setup as False.
    second = await public.request_raw("POST", "/api/setup", json=_setup_body("owner-c"))
    assert second.status_code == 409, f"a second setup must 409: {second.status_code} {second.text}"
    methods_after = await public.get("/api/login/methods")
    assert methods_after["needs_setup"] is False, methods_after


async def test_setup_open_accepts_any_token_and_warns(fresh_stack) -> None:
    # TAI_SETUP_OPEN ungates the door for local/dev: any token initializes, and every boot
    # warns loudly so an open door never ships silently.
    stack = fresh_stack(build_setup_stack, env_overrides={"TAI_SETUP_OPEN": "true"})
    public = _public(stack)

    res = await public.request_raw(
        "POST",
        "/api/setup",
        json={"setup_token": "", "owner_user_id": "owner", "owner_display_name": "owner display"},
    )
    assert res.status_code == 200, f"an open door must accept an absent token: {res.status_code} {res.text}"
    assert res.json()["data"]["api_key"].startswith("sk-"), res.text

    log = stack.process("serve").log_path.read_text(encoding="utf-8", errors="replace")
    assert "TAI_SETUP_OPEN is set" in log, "the open-window boot warning is missing from the serve log"
