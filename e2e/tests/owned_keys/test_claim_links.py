"""One-time claim links (the QR-onboarding carrier): creation rules on the authed
door, and the public exchange leg's once-only burn, uniform-404 no-oracle, reachable-
unauthenticated, revoked-key-dead, and expiry behaviours. The exchange misses must be
byte-identical so the public surface leaks no oracle distinguishing invalid from used."""

from __future__ import annotations

import time
from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

from ._owned_support import mint_owned, mint_owner

# The single uniform exchange-miss message the handler answers for EVERY miss
# (unknown / used / revoked-key / expired). Asserting the body carries it — not
# just a bare 404 status — distinguishes a handler-produced claim miss from a
# framework "route not mounted" 404.
_CLAIM_MISS_MESSAGE = "unknown or already used claim token"


def _no_auth(stack: TaiStack) -> ApiClient:
    """A client carrying NO Authorization header — the public exchange caller."""
    return ApiClient(f"http://{stack.host}:{stack.port_a}")


async def test_claim_link_exchanges_once_and_returns_a_working_key(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    owned_id, owned_raw = await mint_owned(owner, uniq)

    link = await owner.post("/api/auth/claim-links", json={"api_key": owned_raw})
    assert link["claim_path"] == f"/login#claim={link['token']}"

    exchange = _no_auth(auth_stack)
    first = await exchange.request_raw("POST", "/api/login/claim", json={"token": link["token"]})
    assert first.status_code == 200, first.text
    payload = first.json()["data"]
    # The exchange hands back the raw key the link carried and its identity.
    assert payload["token"] == owned_raw
    assert payload["user_id"] == owned_id

    # The handed-out key really authenticates.
    claimed = root.with_token(payload["token"])
    me = await claimed.get("/api/auth/me")
    assert me["user_id"] == owned_id

    # Once-only: a second exchange of the same token is a miss.
    second = await exchange.request_raw("POST", "/api/login/claim", json={"token": link["token"]})
    assert second.status_code == 404


async def test_exchange_misses_are_byte_identical(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    _owned_id, owned_raw = await mint_owned(owner, uniq)
    exchange = _no_auth(auth_stack)

    # An invalid (never-issued) token — the shared baseline every other miss must match.
    invalid = await exchange.request_raw("POST", "/api/login/claim", json={"token": f"clm-{uniq('nope')}"})
    assert invalid.status_code == 404

    # A used token: create, burn once, then exchange again.
    link = await owner.post("/api/auth/claim-links", json={"api_key": owned_raw})
    burned = await exchange.request_raw("POST", "/api/login/claim", json={"token": link["token"]})
    assert burned.status_code == 200
    used = await exchange.request_raw("POST", "/api/login/claim", json={"token": link["token"]})
    assert used.status_code == 404

    # A revoked-key token: create a link over a fresh key, revoke the key, then exchange.
    revoked_id, revoked_raw = await mint_owned(owner, uniq)
    revoked_link = await owner.post("/api/auth/claim-links", json={"api_key": revoked_raw})
    await root.delete(f"/api/auth/api-keys/{revoked_id}")
    revoked = await exchange.request_raw("POST", "/api/login/claim", json={"token": revoked_link["token"]})
    assert revoked.status_code == 404

    # An expired-link token: a short-TTL link waited out WITHOUT exchanging (so it
    # expires, never burns), then exchanged once.
    ttl_seconds = 2
    expiring_link = await owner.post("/api/auth/claim-links", json={"api_key": owned_raw, "ttl_seconds": ttl_seconds})
    created = time.monotonic()

    async def past_ttl() -> bool:
        return time.monotonic() >= created + ttl_seconds + 1.0

    await wait_for_async(past_ttl, deadline=20.0, interval=0.25, message="claim-link TTL window never elapsed")
    expired = await exchange.request_raw("POST", "/api/login/claim", json={"token": expiring_link["token"]})
    assert expired.status_code == 404

    # The no-oracle invariant: EVERY miss body — invalid, used, revoked-key, and
    # expired-link — answers the SAME bytes, so the public surface leaks no oracle
    # distinguishing why a claim missed.
    assert used.content == invalid.content
    assert revoked.content == invalid.content
    assert expired.content == invalid.content


async def test_public_exchange_is_reachable_unauthenticated(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    # No Authorization header at all: the always-public login namespace must let the
    # request REACH the handler (a 404 miss), never bounce it at the gate (401/403).
    response = await _no_auth(auth_stack).request_raw("POST", "/api/login/claim", json={"token": f"clm-{uniq('x')}"})
    assert response.status_code == 404
    # A bare 404 is also what a not-mounted route answers; assert the body carries the
    # HANDLER's uniform claim-miss envelope, proving the public route is genuinely
    # reachable and handled (not a framework not-found).
    assert response.json() == {"error": _CLAIM_MISS_MESSAGE}


async def test_claim_link_creation_rules(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_a_id, owner_a_raw = await mint_owner(root, uniq)
    owner_a = root.with_token(owner_a_raw)
    _owned_a_id, owned_a_raw = await mint_owned(owner_a, uniq)

    _owner_b_id, owner_b_raw = await mint_owner(root, uniq)
    owner_b = root.with_token(owner_b_raw)

    # An unresolvable key → 400 naming it invalid.
    invalid = await owner_a.request_raw("POST", "/api/auth/claim-links", json={"api_key": f"sk-{uniq('nope')}"})
    assert invalid.status_code == 400
    assert "not a valid" in invalid.text.lower()

    # A different owner may not link owner A's owned key (not admin, did not mint it,
    # not its own key) → 403.
    foreign = await owner_b.request_raw("POST", "/api/auth/claim-links", json={"api_key": owned_a_raw})
    assert foreign.status_code == 403

    # Owner A may link its OWN key.
    own = await owner_a.post("/api/auth/claim-links", json={"api_key": owner_a_raw})
    assert own["token"]

    # Owner A may link a key it minted.
    minted = await owner_a.post("/api/auth/claim-links", json={"api_key": owned_a_raw})
    assert minted["token"]


async def test_revoked_key_claim_link_is_dead(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    owned_id, owned_raw = await mint_owned(owner, uniq)

    link = await owner.post("/api/auth/claim-links", json={"api_key": owned_raw})
    # Revoke the underlying key AFTER the link is created: the exchange re-validates
    # and must refuse to hand out a revoked credential (the same uniform 404).
    await root.delete(f"/api/auth/api-keys/{owned_id}")

    response = await _no_auth(auth_stack).request_raw("POST", "/api/login/claim", json={"token": link["token"]})
    assert response.status_code == 404


async def test_claim_link_expires(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    _owned_id, owned_raw = await mint_owned(owner, uniq)
    exchange = _no_auth(auth_stack)
    ttl_seconds = 2

    # (a) BORN-ALIVE sentinel: a FRESH short-TTL link exchanges IMMEDIATELY (200). This
    # catches a born-dead / units bug (e.g. an ``ex``-vs-``px`` mixup that would set a
    # 2ms TTL) — a link that was never exchangeable would already 404 here.
    alive_link = await owner.post("/api/auth/claim-links", json={"api_key": owned_raw, "ttl_seconds": ttl_seconds})
    alive = await exchange.request_raw("POST", "/api/login/claim", json={"token": alive_link["token"]})
    assert alive.status_code == 200, alive.text

    # (b) EXPIRY (not burn): a SEPARATE short-TTL link, waited out WITHOUT ever exchanging
    # it — so the once-only token is never burned — then exchanged ONCE. The 404 can only
    # be expiry, not a spent token. Poll a pure time predicate (no bare sleep — the banned
    # primitive) so nothing touches the token during the wait; ``created`` is captured
    # AFTER the mint returns, so the wall we clear is strictly past the server's TTL start.
    expiring_link = await owner.post("/api/auth/claim-links", json={"api_key": owned_raw, "ttl_seconds": ttl_seconds})
    created = time.monotonic()

    async def past_ttl() -> bool:
        return time.monotonic() >= created + ttl_seconds + 1.0

    await wait_for_async(past_ttl, deadline=20.0, interval=0.25, message="claim-link TTL window never elapsed")
    expired = await exchange.request_raw("POST", "/api/login/claim", json={"token": expiring_link["token"]})
    assert expired.status_code == 404, expired.text
