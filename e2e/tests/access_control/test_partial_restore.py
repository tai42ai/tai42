"""C-access-control — api keys survive a partial restore (Postgres kept, Redis identity flushed).

The mixed disaster-recovery state: the policy rows live in Postgres, but a key's secret
lives ONLY as a hash on the identity record in the Redis identity provider. Lose the
Redis identity records while the Postgres policy rows stand and every key is an ORPHAN —
it can no longer authenticate, yet its policy (scopes, fingerprint, owner claim) survives.

This drives the composed recovery path end to end on the redis identity variant:

* the flushed keys 401 (their identity record is gone);
* the bootstrap door re-opens when only orphans remain (no live key gates it), so an
  operator with no export file still has a way back in;
* the listing flags the surviving rows ``orphaned: true``;
* an orphan is revoked cleanly (its policy row is deleted, gone from the listing);
* minting on an orphaned id is refused with a 400 that names the orphan state;
* importing the ``access_control`` backup re-mints the orphans onto their surviving
  policy rows, returning fresh plaintexts, and a re-minted key authenticates with its
  pre-flush scopes, fingerprint and owner claim intact.

Redis-identity variant only: on the fixture-identity leg the identity records are in
Postgres, so the flush-the-Redis-records mechanism does not model the scenario.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import redis as redis_lib
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# Must equal ``manifests._KEYS_BOOTSTRAP_TOKEN`` (the value the stack env pins), as in
# ``test_keys_bootstrap``.
_TOKEN = "e2e-keys-bootstrap-token"

# The Redis identity provider's record key prefixes — the ONLY keys the partial-restore
# flush removes. ``ac:key:<hash>`` is the key-hash -> identity record; ``ac:management:key:<user_id>``
# is the user_id -> hash reverse lookup. Every other store (policy version, context cache,
# route table lives in Postgres) shares the logical db and MUST survive the flush, so the
# scenario never FLUSHDBs. Prefixes read from the provider code (the identity provider's
# ``key_prefix`` default and its reverse-lookup prefix).
_IDENTITY_KEY_PREFIXES = ("ac:key:", "ac:management:key:")


def _public(stack: TaiStack) -> ApiClient:
    """A client for the stack carrying NO credential — the fresh install has no key."""
    return ApiClient(f"http://{stack.host}:{stack.port_a}")


def _client(stack: TaiStack, token: str) -> ApiClient:
    return ApiClient(f"http://{stack.host}:{stack.port_a}", auth_token=token)


async def _bootstrap(stack: TaiStack, user_id: str) -> str:
    """Mint the first admin key through the public bootstrap door; return the raw ``sk-`` key."""
    public = _public(stack)
    res = await public.request_raw(
        "POST",
        "/api/keys/bootstrap",
        json={"user_id": user_id, "description": f"{user_id} key", "bootstrap_token": _TOKEN},
    )
    assert res.status_code == 200, f"bootstrap for {user_id!r} must 200: {res.status_code} {res.text}"
    key = res.json()["data"]["token"]
    assert key.startswith("sk-"), key
    return key


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


async def test_partial_restore_re_mints_orphaned_keys(keys_bootstrap_backup_stack: TaiStack) -> None:
    stack = keys_bootstrap_backup_stack
    if stack.infra.variants.identity.name != "redis":
        pytest.skip("partial-restore spec models a Redis identity flush; runs only on TAI_E2E_IDENTITY=redis")

    # Bootstrap the first admin, then mint an admin-owned key ``alpha`` (a distinct
    # registered scope so the scope round-trip is real) and a plain key ``beta``.
    root_key = await _bootstrap(stack, "root")
    root = _client(stack, root_key)

    await root.post("/api/auth/scopes", json={"scope_id": "scope-alpha", "url": "/e2e/alpha"})
    alpha_created = await root.post(
        "/api/auth/api-keys",
        json={
            "user_id": "alpha",
            "description": "alpha key",
            "scopes": ["scope-alpha"],
            "owner_user_id": "root",
        },
    )
    alpha_key = alpha_created["api_key"]
    assert alpha_key.startswith("sk-")
    beta_created = await root.post(
        "/api/auth/api-keys",
        json={"user_id": "beta", "description": "beta key", "scopes": ["*"]},
    )
    assert beta_created["api_key"].startswith("sk-")

    # The pre-flush backup: its ``tokens`` carry each policy without key material.
    document = await _export_access_control(root)
    alpha_export = _token_row(document, "alpha")
    assert alpha_export["scopes"] == ["scope-alpha"], alpha_export
    assert alpha_export["policy_data"][OWNER_USER_ID_CLAIM] == "root", alpha_export
    assert alpha_export["policy_data"][KEY_FINGERPRINT_CLAIM], alpha_export
    assert alpha_export["orphaned"] is False, alpha_export

    # Lose the Redis identity records (Postgres policies stand): every key is now an orphan.
    flushed = _flush_identity_records(stack)
    assert any(k.startswith("ac:key:") for k in flushed), flushed
    assert any(k.startswith("ac:management:key:") for k in flushed), flushed

    # Every flushed key 401s — no identity record resolves it.
    for stale in (root_key, alpha_key):
        resp = await _client(stack, stale).request_raw("GET", "/api/auth/me")
        assert resp.status_code == 401, f"a flushed key must 401: {resp.status_code} {resp.text}"

    # Only orphans remain, so the bootstrap door re-opens: mint a NEW live admin to drive recovery.
    root2_key = await _bootstrap(stack, "root-2")
    admin = _client(stack, root2_key)

    # The listing flags each surviving row orphaned; the fresh admin is not an orphan.
    listing = await admin.get("/api/auth/tokens-payload")
    for orphan_id in ("root", "alpha", "beta"):
        row = _listing_row(listing, orphan_id)
        assert row is not None, f"{orphan_id!r} missing from listing: {listing}"
        assert row.get("orphaned") is True, f"{orphan_id!r} must be listed orphaned: {row}"
    root2_row = _listing_row(listing, "root-2")
    assert root2_row is not None, listing
    assert root2_row.get("orphaned") is False, root2_row

    # Revoke an orphan: its policy row is deleted, and it is gone from the listing.
    revoke = await admin.request_raw("DELETE", "/api/auth/api-keys/beta")
    assert revoke.status_code == 200, f"revoke of an orphan must 200: {revoke.status_code} {revoke.text}"
    after_revoke = await admin.get("/api/auth/tokens-payload")
    assert _listing_row(after_revoke, "beta") is None, f"beta must be gone after revoke: {after_revoke}"

    # Minting on an orphaned id is refused with a 400 naming the orphan state (never an auto-heal).
    refused = await admin.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": "root", "description": "retry", "scopes": ["*"]},
    )
    assert refused.status_code == 400, f"mint on an orphan id must 400: {refused.status_code} {refused.text}"
    assert "orphaned" in refused.text, refused.text

    # Import the pre-flush backup. The still-orphaned rows (root, alpha) are re-minted onto
    # their surviving policy rows; the revoked beta — its policy row deleted, so ``absent`` —
    # is minted fresh from its backup token. The backup is the source of truth and the
    # access_control section keeps NO revocation tombstone, so all three come back with a new
    # plaintext (contrast the webhooks section, which tombstones a revoked link across restore).
    result = await admin.post("/api/backup/import", json={"document": document, "sections": ["access_control"]})
    ac_report = result["sections"]["access_control"]
    new_by_user = {entry["user_id"]: entry["api_key"] for entry in ac_report["new_api_keys"]}
    assert new_by_user.keys() == {"root", "alpha", "beta"}, ac_report
    assert ac_report["created"] == 3, ac_report

    # A re-minted orphan authenticates with its NEW plaintext and its policy is intact:
    # scopes, fingerprint claim and owner claim equal the exported row (the policy row
    # was never touched by the re-mint).
    alpha_reminted = _client(stack, new_by_user["alpha"])
    me = await alpha_reminted.get("/api/auth/me")
    assert me is not None, me
    post_listing = await admin.get("/api/auth/tokens-payload")
    alpha_now = _listing_row(post_listing, "alpha")
    assert alpha_now is not None, post_listing
    assert alpha_now.get("orphaned") is False, alpha_now
    assert alpha_now["scopes"] == alpha_export["scopes"], (alpha_now, alpha_export)
    assert alpha_now["policy_data"][KEY_FINGERPRINT_CLAIM] == alpha_export["policy_data"][KEY_FINGERPRINT_CLAIM]
    assert alpha_now["policy_data"][OWNER_USER_ID_CLAIM] == alpha_export["policy_data"][OWNER_USER_ID_CLAIM]
