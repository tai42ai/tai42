"""§3b — operator session creds reach the CLEAN ``claude_code`` session env, and fail CLOSED.

The plugin injects ONLY its operator ``creds`` list (plus the one model credential) into a
CLEAN session env — never the host env. Two invariants are proven deterministically over the
fake sandbox:

* REACH — a static ``delivery``-less cred (``StaticCred``, baked into the create-time env) is
  read back OUT of the shell by the scripted runner: it reads ``E2E_SVC_TOKEN`` off its own
  process env and emits it as the answer, and the test asserts the KNOWN token round-trips, so
  it demonstrably reached the session env. This is the static-env channel; the refreshable
  ``delivery="bearer"`` per-turn credential-file path is proven in the plugin's own unit tests.
* FAIL-CLOSED — a per-caller connection-reference cred (``ConnectionCred``) on a door with NO
  bound execution identity (the anonymous tool face and SSE route on this stack) causes a LOUD
  RAISE at session setup. It is ``resolve_connection_auth`` that refuses when no execution
  identity is bound (the skeleton seam's fail-close), NOT the agent detecting identity itself,
  and no cred is injected — never a silent drop. Deterministic: no real vendor key, the refusal
  fires before any connection lookup.

The identity-BOUND positive of the connection-reference channel (the token resolves and is
injected under the caller's identity) needs an execution identity + a connectors provider,
neither wired on ``build_claude_agent_stack``; it is covered in the plugin's own unit tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._claude_support import claude_stack, error_text, frames_of_type, run_sse

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("claude_agent"),
        reason="scripted runner stub is the 'claude_agent' mock leg; the real turn is the §5 smoke",
    ),
]

# The known constant the static cred carries; the runner reads it off ``E2E_SVC_TOKEN`` and
# emits it back, so its round-trip proves it reached the CLEAN session env.
_KNOWN_TOKEN = "known-session-token-a1b2c3"

# A per-caller connection reference over a fixture provider id — no real vendor secret. On an
# identity-less door it never resolves: the skeleton seam fails closed first.
_CONNECTION_CRED = {
    "kind": "connection",
    "env_name": "E2E_SVC_TOKEN",
    "connection_id": "e2e-conn",
    "provider_id": "e2e_idp",
    "sub_service": "default",
    "delivery": "env",
    "required": True,
}


def _static_creds() -> str:
    return json.dumps([{"kind": "static", "env_name": "E2E_SVC_TOKEN", "value": _KNOWN_TOKEN}])


def _connection_creds() -> str:
    return json.dumps([_CONNECTION_CRED])


async def test_static_cred_reaches_the_clean_session_env(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "session_creds", TAI_AGENTS_CLAUDE_CREDS=_static_creds())
    async with stack.mcp() as mcp:
        result = await mcp.call_tool("claude_code", {"user_message": "hi"}, retry_on_reloading=True)
    # The runner echoed ``E2E_SVC_TOKEN`` from its session env; the known token round-tripped.
    assert result.data == _KNOWN_TOKEN, result.data


async def test_connection_cred_on_identity_less_tool_face_fails_closed(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "session_creds", TAI_AGENTS_CLAUDE_CREDS=_connection_creds())
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code", {"user_message": "hi"}, retry_on_reloading=True, raise_on_error=False
        )
    assert result.is_error, result.data
    assert "no execution identity bound" in error_text(result), error_text(result)


async def test_connection_cred_on_identity_less_sse_route_fails_closed(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "session_creds", TAI_AGENTS_CLAUDE_CREDS=_connection_creds())
    frames = await run_sse(stack, {"user_message": "hi"})
    errors = frames_of_type(frames, "stream.error")
    assert errors, frames
    assert "no execution identity bound" in errors[0]["message"], errors
