"""§3b — the ``claude_code`` adapter-proxied platform-tool bridge.

The adapter proxies a runner ``tool_call`` back to a REAL platform tool via
``tai42_app.tools.run_tool`` UNDER THE TURN'S execution identity, validating the call against
the requested ``tool_names`` allowlist — no platform network endpoint, no in-session
credential. That proxy is GATED on a bound execution identity: a non-empty ``tool_names`` on a
door with none is refused before any ``tool_call`` can be issued, so the bridge never runs a
tool the platform cannot entitlement-check.

Reachable here (both identity-less doors on ``build_claude_agent_stack``): the proxy is
fenced — a ``tool_names`` run whose runner WOULD emit a ``tool_call`` is refused at the door,
so the bridge is provably closed without an identity.

The POSITIVE round-trip — the runner emits a ``tool_call`` for an allowlisted probe, the
adapter runs it under the caller's identity and returns the ``tool_result`` (with the
``is_error`` round-trip), and two concurrent ``tool_call`` frames route by ``call_id`` under
single-writer discipline, an out-of-allowlist name raising loudly — needs a bound execution
identity, which this stack does not provide (auth off, no identity provider). It is covered in
the plugin's own drive tests and is skipped here with that reason.
"""

from __future__ import annotations

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


async def test_the_tool_proxy_is_identity_gated_on_both_doors(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    # The ``tool_call`` runner would proxy ``e2e_echo`` back through the platform; on an
    # identity-less door the adapter refuses the run before the proxy can ever run a tool.
    stack = claude_stack(fresh_stack, llm_stub, "tool_call")

    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code",
            {"user_message": "hi", "tool_names": ["e2e_echo"]},
            retry_on_reloading=True,
            raise_on_error=False,
        )
    assert result.is_error, result.data
    assert "no bound execution identity" in error_text(result), error_text(result)

    frames = await run_sse(stack, {"user_message": "hi", "tool_names": ["e2e_echo"]})
    errors = frames_of_type(frames, "stream.error")
    assert errors, frames
    assert "no bound execution identity" in errors[0]["message"], errors


@pytest.mark.skip(
    reason="the positive adapter-proxy round-trip (a tool_call proxied to run_tool under the "
    "turn's identity, the is_error round-trip, two concurrent tool_calls routed by call_id, and "
    "the out-of-allowlist rejection) needs a bound execution identity, which "
    "build_claude_agent_stack does not wire; it is covered in the plugin's own drive tests"
)
async def test_proxied_tool_round_trip_under_identity() -> None:  # pragma: no cover - documented gap
    ...
