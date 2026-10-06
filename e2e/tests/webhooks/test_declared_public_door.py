"""The webhook ingress door is PUBLIC BY DECLARATION on a real access-controlled deployment.

The stripe stack runs access control ON with NO route row for the webhook ingress (see
``seed_stripe_authz``): the door is reachable only because it registers ``authed=False``, so
the verifier's declared-public tier publics it above the route table. A bound topic's verifier
is then the door's only lock. This proves, end to end, the operator's named contract: an
unsigned delivery to a verifier-bound topic is refused by the TOPIC VERIFIER (401), never by
access control (403 "Forbidden: Route not configured") — if the door were not public by
declaration, the row's absence would 403 it before the verifier ever ran.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "process",
        "files",
        "kind:webhook_verifiers:stripe",
        "setting:E2E_STRIPE_WEBHOOK_SECRET=known-to-test",
    ),
]


def _sign(secret: bytes, body: bytes, *, timestamp: int) -> str:
    """A Stripe-Signature header value over ``"<timestamp>.<body>"``, as the verifier recomputes it."""
    digest = hmac.new(secret, f"{timestamp}".encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


async def test_webhook_door_is_public_by_declaration(
    stripe_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = stripe_stack
    secret = stack.config.env["E2E_STRIPE_WEBHOOK_SECRET"].encode()
    api = stack.api(port=stack.port_a)

    topic = uniq("topic").replace("_", "-")
    await api.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={"verifier": "stripe", "config": {"secret_env": "E2E_STRIPE_WEBHOOK_SECRET"}},
    )

    body = b'{"id":"evt_test","type":"checkout.session.completed"}'
    path = f"/universal_webhook/{topic}"

    # (a) UNSIGNED POST to the verifier-bound topic: admitted by declaration (no 403 from
    # access control, which has no row for this door), then REFUSED by the topic verifier — 401.
    unsigned = await api.request_raw("POST", path, content=body)
    assert unsigned.status_code == 401, unsigned.text
    assert "webhook verification failed" in unsigned.text

    # (b) A correctly-signed POST passes the verifier and is accepted.
    signed = _sign(secret, body, timestamp=int(time.time()))
    accepted = await api.request_raw("POST", path, headers={"Stripe-Signature": signed}, content=body)
    assert accepted.status_code == 200, accepted.text

    # (c) A fresh UNBOUND topic has no verifier, so the declared-public door accepts straight away.
    unbound_topic = uniq("unbound").replace("_", "-")
    unbound = await api.request_raw("POST", f"/universal_webhook/{unbound_topic}", content=body)
    assert unbound.status_code == 200, unbound.text
