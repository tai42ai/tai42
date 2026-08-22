"""``append_thread_messages`` takes NO sandbox — the checkpoint-only history write (§B3.5).

The durable run/astream drive carries a HARD sandbox dependency, but ``append_thread_messages``
is a checkpoint-only write that never calls ``require_sandbox`` (``session=None``, the non-sandbox
``StateBackend``). This asserts that end to end on the messaging-bridge stack, which registers
``langchain_deep_agent`` alongside the conversations door but installs NO sandbox provider: a
MANUAL-mode inbound to the deep-agent target appends the inbound to the agent's thread memory
WITHOUT a sandbox — a run turn on the same box WOULD raise ``SandboxUnavailableError``, so a
successful manual-mode append proves the append path is genuinely sandbox-free.

Manual mode runs no target turn: the inbound is appended as a ``user`` message via
``append_thread_messages`` and the outcome is silent. A sandbox requirement would surface as a
loud ``manual-mode append error`` outcome instead, so the SILENT outcome is the proof.

This is the ONE leg on the bridge stack (auth-on, conversations, backend, no sandbox) — the only
foundation composition that reaches the deep agent's ``append_thread_messages`` door AND runs it
without a sandbox. The bearer/session-cred and durable-workspace legs need a sandbox provider,
which no bridge-door stack composes; they ride the fake-sandbox durable stack instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# Auth-on bridge stack + scripted-LLM + channel stubs are the mock leg; step aside on any real
# seam that would break the scripting/stubs.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("claude_agent"),
    reason="the scripted-LLM bridge stack is the mock leg; the real leg runs on the creds host",
)

_AGENT = "langchain_deep_agent"


async def _mint_key(stack: TaiStack, root_token: str, user_id: str) -> str:
    created = (
        await stack.api(port=stack.port_b)
        .with_token(root_token)
        .post(
            "/api/auth/api-keys",
            json={"user_id": user_id, "description": "e2e deep-append key", "scopes": ["e2e-all"]},
        )
    )
    raw = created["api_key"] if isinstance(created, dict) else created
    assert isinstance(raw, str), f"unexpected mint response: {created!r}"
    assert raw.startswith("sk-"), f"unexpected mint token: {raw!r}"
    return raw


async def test_manual_mode_append_to_the_deep_agent_needs_no_sandbox(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, root_token = bridge_stack
    api = stack.api(port=stack.port_b).with_token(root_token)

    execution_user = uniq("deep-append-exec")
    await _mint_key(stack, root_token, execution_user)

    route_name = uniq("deep-append-route").replace("_", "-")
    # An api-door route to the deep-agent target, MANUAL from the start: a manual inbound runs no
    # agent turn — it only appends to thread memory via append_thread_messages.
    await api.post(
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "agent",
            "target_name": _AGENT,
            "execution_key": execution_user,
            # An unreachable https loopback callback: manual mode is silent, so the callback is
            # never fired — the door only validates it is an absolute https URL.
            "callback_url": "https://127.0.0.1:9/e2e-deep-append-callback",
            "initial_mode": "manual",
        },
    )

    end_user = uniq("deep-append-user")
    remembered = uniq("remember-this")
    # A manual-mode inbound: appended to the deep agent's thread memory (no sandbox), silent
    # outcome. wait_seconds resolves it inline so the outcome is observable.
    outcome = await api.post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": end_user, "text": remembered, "wait_seconds": 20},
    )
    # The manual-mode append succeeded WITHOUT a sandbox: a silent outcome (the sync-wait payload
    # rides ``answer``), never a loud ``manual-mode append error`` (how a sandbox requirement would
    # surface).
    assert outcome["answer"] is not None, f"the manual-mode turn did not resolve inline: {outcome}"
    assert outcome["answer"]["status"] == "silent", (
        f"the manual-mode deep-agent append did not run sandbox-free: {outcome}"
    )
    assert "append error" not in json.dumps(outcome), f"the append surfaced an error outcome: {outcome}"

    # The inbound landed in the thread's memory: the transcript for this route's single thread
    # carries the remembered line — the append genuinely wrote, it was not a silent no-op.
    threads = await api.get(f"/api/conversations/{route_name}/threads")
    (thread,) = threads["items"]
    thread_id = thread["thread_id"]
    transcript = await api.get(f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}")
    assert remembered in json.dumps(transcript), f"the manual inbound was not appended to the thread: {transcript}"
