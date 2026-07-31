"""C7 (P1) — a hook bound on replica A fires its tool from a universal-webhook
delivery on replica B; and the real github verifier locks a topic to signed
deliveries."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from tai42_e2e import wait_for
from tai42_e2e.stack import TaiStack


async def test_hook_bound_on_a_fires_tool_from_webhook_on_b(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    await api_a.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "e2e_record",
            "tool_kwargs": {"key": rkey, "value": "fired"},
            "execution_key": uniq("exec"),
        },
    )
    await api_b.request_raw("POST", f"/universal_webhook/{topic}", json={"any": "payload"})

    wait_for(
        lambda: _has_record(replicas_stack, rkey),
        deadline=5.0,
        message="the hook bound on A never fired from the webhook on B",
    )


async def test_github_verifier_locks_topic(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    secret = replicas_stack.config.env["E2E_GH_WEBHOOK_SECRET"].encode()
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    await api_a.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "e2e_record",
            "tool_kwargs": {"key": rkey, "value": "ok"},
            "execution_key": uniq("exec"),
        },
    )
    await api_a.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={"verifier": "github", "config": {"secret_env": "E2E_GH_WEBHOOK_SECRET"}},
    )

    body = b'{"delivery":"signed"}'
    good_sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    path = f"/universal_webhook/{topic}"

    # A correctly signed POST is accepted and the record appears.
    accepted = await api_b.request_raw("POST", path, headers={"X-Hub-Signature-256": good_sig}, content=body)
    assert accepted.status_code == 200
    wait_for(lambda: _has_record(replicas_stack, rkey), deadline=5.0, message="signed delivery did not fire the tool")
    baseline = len(replicas_stack.records(rkey))

    # A tampered body (stale signature) is rejected — no new record.
    tampered = await api_b.request_raw("POST", path, headers={"X-Hub-Signature-256": good_sig}, content=body + b"x")
    assert tampered.status_code >= 400
    # A missing signature is rejected.
    unsigned = await api_b.request_raw("POST", path, content=body)
    assert unsigned.status_code >= 400
    # A GET delivery is rejected (post_only).
    get_delivery = await api_b.request_raw("GET", path)
    assert get_delivery.status_code >= 400

    assert len(replicas_stack.records(rkey)) == baseline, "a rejected delivery still fired the tool"


async def test_shared_secret_verifier_locks_topic(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    # The builtin shared_secret verifier compares a named header to the value of the
    # env var it is bound to; reuse the per-stack E2E_GH_WEBHOOK_SECRET as that value.
    secret = replicas_stack.config.env["E2E_GH_WEBHOOK_SECRET"]
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    await api_a.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "e2e_record",
            "tool_kwargs": {"key": rkey, "value": "ok"},
            "execution_key": uniq("exec"),
        },
    )
    await api_a.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={"verifier": "shared_secret", "config": {"header": "X-E2E-Secret", "secret_env": "E2E_GH_WEBHOOK_SECRET"}},
    )

    path = f"/universal_webhook/{topic}"

    # A delivery carrying the correct secret header is accepted and the record appears.
    accepted = await api_b.request_raw("POST", path, headers={"X-E2E-Secret": secret}, json={"delivery": "ok"})
    assert accepted.status_code == 200
    wait_for(
        lambda: _has_record(replicas_stack, rkey), deadline=5.0, message="a correctly-secreted delivery did not fire"
    )
    baseline = len(replicas_stack.records(rkey))

    # A wrong secret is rejected (401) — the header is present but does not match.
    wrong = await api_b.request_raw("POST", path, headers={"X-E2E-Secret": "wrong"}, json={"delivery": "ok"})
    assert wrong.status_code >= 400
    # A missing header is rejected (401) — the verifier is header-based, so no header
    # is a verification failure, not a misconfiguration.
    missing = await api_b.request_raw("POST", path, json={"delivery": "ok"})
    assert missing.status_code >= 400

    assert len(replicas_stack.records(rkey)) == baseline, "a rejected delivery still fired the tool"


def _has_record(stack: TaiStack, key: str) -> bool:
    return len(stack.records(key)) > 0
