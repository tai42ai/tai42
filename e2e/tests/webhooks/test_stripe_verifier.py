"""The stripe webhook verifier leg: a topic bound to the ``stripe`` verifier locks to
signed POST delivery, and every rejection carries the door's EXACT status code.

Rides the shared ``replicas_stack`` (which loads ``tai42_webhook_verifier_stripe`` under
the canonical ``webhook_verifier_modules`` field and holds ``E2E_STRIPE_WEBHOOK_SECRET``).
Deliveries are signed locally as Stripe signs them — ``t=<ts>,v1=<hmac_sha256(secret,
"<ts>.<raw body>")>`` — so the plugin's real signature verification runs against them, never
a bypass. Stricter than ``test_universal_webhook``'s ``>= 400``: the codes are the door's
own (``405`` post-only, ``401`` verify-failed), and the strictness is the point."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable

from tai42_e2e.stack import TaiStack


def _sign(secret: bytes, body: bytes, *, timestamp: int) -> str:
    """A Stripe-Signature header value over ``"<timestamp>.<body>"``, exactly as the
    verifier recomputes it."""
    digest = hmac.new(secret, f"{timestamp}".encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


async def test_stripe_verifier_locks_topic(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    topic = uniq("topic").replace("_", "-")
    secret = replicas_stack.config.env["E2E_STRIPE_WEBHOOK_SECRET"].encode()
    api = replicas_stack.api(port=replicas_stack.port_a)

    # Bind the stripe verifier to the topic; from here the door verifies every delivery.
    await api.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={"verifier": "stripe", "config": {"secret_env": "E2E_STRIPE_WEBHOOK_SECRET"}},
    )

    body = b'{"id":"evt_test","type":"checkout.session.completed"}'
    path = f"/universal_webhook/{topic}"

    # Unsigned POST: no Stripe-Signature header -> the verifier fails closed with 401.
    unsigned = await api.request_raw("POST", path, content=body)
    assert unsigned.status_code == 401, unsigned.text

    # Correctly signed POST: accepted (200). No hook is registered, so the door verifies,
    # dispatches nothing, and answers the ingress 200.
    good = _sign(secret, body, timestamp=int(time.time()))
    accepted = await api.request_raw("POST", path, headers={"Stripe-Signature": good}, content=body)
    assert accepted.status_code == 200, accepted.text

    # GET on the verified topic: the stripe verifier is post_only, so the door refuses a GET
    # with 405 before any signature is considered.
    get_delivery = await api.request_raw("GET", path)
    assert get_delivery.status_code == 405, get_delivery.text

    # Signed with a stale timestamp (older than the default 300s tolerance): a valid HMAC over
    # a too-old payload is a replay and is rejected 401.
    stale = _sign(secret, body, timestamp=int(time.time()) - 400)
    stale_resp = await api.request_raw("POST", path, headers={"Stripe-Signature": stale}, content=body)
    assert stale_resp.status_code == 401, stale_resp.text
