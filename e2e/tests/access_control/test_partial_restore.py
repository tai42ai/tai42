"""C-access-control — api keys survive a partial restore (Postgres kept, Redis identity flushed).

The mixed disaster-recovery state: the policy rows live in Postgres, but a key's secret
lives ONLY as a hash on the identity record in the Redis identity provider. Lose the Redis
identity records while the Postgres policy rows stand and every key is an ORPHAN — it can no
longer authenticate, yet its policy (scopes, fingerprint, owner claim) survives, and the
owner principal keeps ``POST /api/setup`` shut (409, a principal exists).

This drives BOTH composed recovery paths end to end on the redis identity variant:

* the NO-EXPORT path — the host-side ``tai setup --recover`` re-mints ONE surviving owner key
  onto its policy row, the way back in when no backup file exists;
* the export path — importing the ``access_control`` backup re-mints the remaining orphans
  onto their surviving policy rows.

Redis-identity variant only: on the fixture-identity leg the identity records are in
Postgres, so the flush-the-Redis-records mechanism does not model the scenario.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, cast

import pytest
import redis as redis_lib
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM

from tai42_e2e.binaries import tai_bin
from tai42_e2e.child_env import child_env
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import _SETUP_TOKEN
from tai42_e2e.stack import TaiStack

# The Redis identity provider's record key prefixes — the ONLY keys the partial-restore
# flush removes. ``ac:key:<hash>`` is the key-hash -> identity record; ``ac:management:key:<user_id>``
# is the user_id -> hash reverse lookup. Every other store (policy version, context cache,
# route table lives in Postgres) shares the logical db and MUST survive the flush, so the
# scenario never FLUSHDBs.
_IDENTITY_KEY_PREFIXES = ("ac:key:", "ac:management:key:")


def _public(stack: TaiStack) -> ApiClient:
    """A client for the stack carrying NO credential — the fresh install has no key."""
    return ApiClient(f"http://{stack.host}:{stack.port_a}")


def _client(stack: TaiStack, token: str) -> ApiClient:
    return ApiClient(f"http://{stack.host}:{stack.port_a}", auth_token=token)


async def _setup(stack: TaiStack, owner_id: str) -> dict[str, Any]:
    """Initialize the deployment through the public setup door; return the setup result."""
    public = _public(stack)
    res = await public.request_raw(
        "POST",
        "/api/setup",
        json={
            "setup_token": _SETUP_TOKEN,
            "owner_user_id": owner_id,
            "owner_display_name": f"{owner_id} display",
            "key_user_id": f"{owner_id}-key",
        },
    )
    assert res.status_code == 200, f"setup for {owner_id!r} must 200: {res.status_code} {res.text}"
    data = res.json()["data"]
    assert data["api_key"].startswith("sk-"), data
    return data


def _flush_identity_records(stack: TaiStack) -> list[str]:
    """Delete ONLY the Redis identity provider's record keys from the stack's logical db,
    modelling a redis restore that lost the identity store while Postgres kept the policies.

    Returns the sorted keys removed (the flush evidence)."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    removed: list[str] = []
    try:
        for prefix in _IDENTITY_KEY_PREFIXES:
            keys = sorted(cast("list[str]", list(client.scan_iter(match=f"{prefix}*"))))
            for key in keys:
                client.delete(key)
                removed.append(key)
    finally:
        client.close()
    return sorted(removed)


def _run_tai(stack: TaiStack, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the real ``tai`` console script against the deployment through the harness's own
    child-env seam — the SAME env the served processes get (the feature env plus the
    config-dir/manifest bindings and PATH/HOME, never an ``os.environ`` passthrough) — with
    the config dir as the working directory."""
    config_dir = str(stack._config_dir)
    return subprocess.run(
        [tai_bin(), *argv],
        env=child_env(stack, str(stack.root), config_dir),
        cwd=config_dir,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def _export_access_control(admin: ApiClient) -> dict[str, Any]:
    """The raw ``access_control`` backup document (NOT the ``{data}`` envelope)."""
    resp = await admin.request_raw("POST", "/api/backup/export", json={"sections": ["access_control"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _token_row(document: dict[str, Any], user_id: str) -> dict[str, Any]:
    tokens = document["sections"]["access_control"]["tokens"]
    row = next((t for t in tokens if t.get("user_id") == user_id), None)
    assert row is not None, f"{user_id!r} missing from export tokens: {tokens}"
    return row


def _listing_row(payload: list[dict[str, Any]], user_id: str) -> dict[str, Any] | None:
    return next((p for p in payload if p.get("user_id") == user_id), None)


async def _flush_and_prove_lockout(stack: TaiStack, setup_data: dict[str, Any], alpha_created: dict[str, Any]) -> None:
    """Lose the Redis identity records (Postgres policies stand), then prove every key is an
    orphan: the flush removed both record kinds, the flushed keys 401, and the surviving owner
    principal keeps the setup door shut (409, ``needs_setup`` false)."""
    flushed = _flush_identity_records(stack)
    assert any(k.startswith("ac:key:") for k in flushed), flushed
    assert any(k.startswith("ac:management:key:") for k in flushed), flushed

    # Every flushed key 401s — no identity record resolves it.
    for stale in (setup_data["api_key"], alpha_created["api_key"]):
        resp = await _client(stack, stale).request_raw("GET", "/api/auth/me")
        assert resp.status_code == 401, f"a flushed key must 401: {resp.status_code} {resp.text}"

    # The owner principal survives in Postgres, so the setup door stays shut — no way back in
    # through it, and needs_setup stays false.
    reinit = await _public(stack).request_raw(
        "POST", "/api/setup", json={"setup_token": _SETUP_TOKEN, "owner_user_id": "root", "owner_display_name": "x"}
    )
    assert reinit.status_code == 409, f"setup must stay shut once the owner exists: {reinit.status_code} {reinit.text}"
    methods = await _public(stack).get("/api/login/methods")
    assert methods["needs_setup"] is False, methods


async def _recover_owner_key(stack: TaiStack, pre_flush_fingerprint: str, pre_flush_scopes: list[str]) -> ApiClient:
    """NO-EXPORT recovery: the host-side command re-mints ONE named owner key onto its
    surviving policy row. Prove the re-minted key authenticates as a full admin projecting the
    owner, its row is live again with the pre-flush fingerprint and scopes, and alpha and beta
    stay orphaned. Returns the admin client the re-minted key authenticates."""
    # The owner holds several keys (root-key, alpha, beta), so name the one to re-mint.
    recover = _run_tai(stack, ["--json", "setup", "--recover", "--token", _SETUP_TOKEN, "--key-user", "root-key"])
    assert recover.returncode == 0, f"recover must exit 0: {recover.returncode}\n{recover.stdout}\n{recover.stderr}"
    recovered = json.loads(recover.stdout)
    assert recovered["owner_user_id"] == "root", recovered
    assert recovered["key_user_id"] == "root-key", recovered
    assert recovered["api_key"].startswith("sk-"), recovered

    # The re-minted owner key authenticates as a full admin projecting the owner principal.
    admin = _client(stack, recovered["api_key"])
    me = await admin.get("/api/auth/me")
    assert me["admin"] is True, me
    assert me["principal"]["user_id"] == "root", me

    # The listing: the owner key is live again with its pre-flush fingerprint and scopes, while
    # alpha and beta stay orphaned (recovery re-mints exactly one row).
    listing = await admin.get("/api/auth/tokens-payload")
    root_key_row = _listing_row(listing, "root-key")
    assert root_key_row is not None, listing
    assert root_key_row["orphaned"] is False, root_key_row
    assert root_key_row["policy_data"][KEY_FINGERPRINT_CLAIM] == pre_flush_fingerprint, root_key_row
    assert root_key_row["scopes"] == pre_flush_scopes, root_key_row
    for orphan_id in ("alpha", "beta"):
        row = _listing_row(listing, orphan_id)
        assert row is not None, (orphan_id, listing)
        assert row["orphaned"] is True, (orphan_id, row)
    return admin


async def _prove_recovery_refusals(stack: TaiStack, admin: ApiClient) -> None:
    """The recovery refusals: a second recover is refused (a live key exists), a wrong token is
    a generic Forbidden, a revoked orphan leaves the listing, and a mint on a still-orphaned id
    is refused with a 400 naming the orphan state."""
    # A second recovery is refused: the owner already holds a live key now.
    again = _run_tai(stack, ["--json", "setup", "--recover", "--token", _SETUP_TOKEN, "--key-user", "root-key"])
    assert again.returncode == 1, f"a second recover must exit 1: {again.returncode}\n{again.stdout}\n{again.stderr}"
    assert "already holds a live key" in again.stderr, again.stderr

    # A wrong token is a generic Forbidden — no oracle for the initialized/recoverable state.
    wrong = _run_tai(stack, ["--json", "setup", "--recover", "--token", "nope", "--key-user", "root-key"])
    assert wrong.returncode == 1, f"a wrong-token recover must exit 1: {wrong.returncode}\n{wrong.stderr}"
    assert "Forbidden" in wrong.stderr, wrong.stderr

    # Revoke an orphan cleanly (its policy row is deleted, gone from the listing), and a mint
    # on the remaining orphan id is refused with a 400 naming the orphan state.
    revoke = await admin.request_raw("DELETE", "/api/auth/api-keys/beta")
    assert revoke.status_code == 200, f"revoke of an orphan must 200: {revoke.status_code} {revoke.text}"
    assert _listing_row(await admin.get("/api/auth/tokens-payload"), "beta") is None
    refused = await admin.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": "alpha", "description": "retry", "scopes": ["*"], "owner_user_id": "root"},
    )
    assert refused.status_code == 400, refused.text
    assert "orphaned" in refused.text, refused.text


async def _prove_export_reimport_remints_orphans(
    stack: TaiStack, admin: ApiClient, document: dict[str, Any], alpha_export: dict[str, Any]
) -> None:
    """EXPORT recovery: importing the pre-flush backup re-mints the remaining orphans onto their
    surviving policy rows — alpha (still orphaned) and the revoked beta (recreated from its
    backup token) — while the already-recovered owner key is left in place. A re-minted orphan
    authenticates with its NEW plaintext, its policy (scopes, fingerprint, owner claim) intact."""
    result = await admin.post("/api/backup/import", json={"document": document, "sections": ["access_control"]})
    ac_report = result["sections"]["access_control"]
    new_by_user = {entry["user_id"]: entry["api_key"] for entry in ac_report["new_api_keys"]}
    assert new_by_user.keys() == {"alpha", "beta"}, ac_report
    assert ac_report["created"] == 2, ac_report

    # A re-minted orphan authenticates with its NEW plaintext, its policy intact (scopes,
    # fingerprint and owner claim equal the exported row — the policy row was never touched).
    alpha_reminted = _client(stack, new_by_user["alpha"])
    assert (await alpha_reminted.get("/api/auth/me")) is not None
    alpha_now = _listing_row(await admin.get("/api/auth/tokens-payload"), "alpha")
    assert alpha_now is not None, alpha_now
    assert alpha_now["orphaned"] is False, alpha_now
    assert alpha_now["scopes"] == alpha_export["scopes"], (alpha_now, alpha_export)
    assert alpha_now["policy_data"][KEY_FINGERPRINT_CLAIM] == alpha_export["policy_data"][KEY_FINGERPRINT_CLAIM]
    assert alpha_now["policy_data"][OWNER_USER_ID_CLAIM] == alpha_export["policy_data"][OWNER_USER_ID_CLAIM]


async def test_partial_restore_recovers_the_owner_key_and_re_mints_orphans(setup_backup_stack: TaiStack) -> None:
    stack = setup_backup_stack
    if stack.infra.variants.identity.name != "redis":
        pytest.skip("partial-restore spec models a Redis identity flush; runs only on TAI_E2E_IDENTITY=redis")

    # Initialize the deployment (owner ``root``, key ``root-key``), then mint an owner-owned
    # key ``alpha`` (a distinct registered scope so the scope round-trip is real) and a plain
    # ``beta`` — every key belongs to the ``root`` owner.
    setup_data = await _setup(stack, "root")
    root = _client(stack, setup_data["api_key"])

    await root.post("/api/auth/scopes", json={"scope_id": "scope-alpha", "url": "/e2e/alpha"})
    alpha_created = await root.post(
        "/api/auth/api-keys",
        json={"user_id": "alpha", "description": "alpha key", "scopes": ["scope-alpha"], "owner_user_id": "root"},
    )
    assert alpha_created["api_key"].startswith("sk-")
    beta_created = await root.post(
        "/api/auth/api-keys",
        json={"user_id": "beta", "description": "beta key", "scopes": ["*"], "owner_user_id": "root"},
    )
    assert beta_created["api_key"].startswith("sk-")

    # The pre-flush backup: its ``tokens`` carry each policy without key material.
    document = await _export_access_control(root)
    root_key_export = _token_row(document, "root-key")
    alpha_export = _token_row(document, "alpha")
    assert alpha_export["scopes"] == ["scope-alpha"], alpha_export
    assert alpha_export["policy_data"][OWNER_USER_ID_CLAIM] == "root", alpha_export
    pre_flush_fingerprint = root_key_export["policy_data"][KEY_FINGERPRINT_CLAIM]
    pre_flush_scopes = root_key_export["scopes"]
    assert pre_flush_scopes == ["*"], root_key_export

    await _flush_and_prove_lockout(stack, setup_data, alpha_created)
    admin = await _recover_owner_key(stack, pre_flush_fingerprint, pre_flush_scopes)
    await _prove_recovery_refusals(stack, admin)
    await _prove_export_reimport_remints_orphans(stack, admin, document, alpha_export)
