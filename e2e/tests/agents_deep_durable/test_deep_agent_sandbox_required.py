"""The durable scratch backend is a HARD sandbox dependency (§B3.7).

``langchain_deep_agent``'s scratch moved onto a durable sandbox WORKSPACE volume, so a
run/astream drive acquires the session BEFORE the graph compiles and raises the every-door
``SandboxUnavailableError`` at the ``require_sandbox`` chokepoint when no provider is installed.

This rides a FRESH no-sandbox variant (the durable stack composed off the foundation builder with
the scalar ``sandbox_module`` slot dropped), booted function-scoped so it never contends with the
module-scoped durable stack for the single checkpoint logical DB. The agent still REGISTERS with
no provider — its digest-``session_image`` validation and the sandbox dependency both fire at RUN
start, never at plugin import.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

from ._support import AGENT, DETERMINISTIC_MARKS, build_deep_durable_no_sandbox_stack

pytestmark = DETERMINISTIC_MARKS


async def test_run_without_a_sandbox_provider_raises_loudly(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(
        build_deep_durable_no_sandbox_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        allocate_checkpoint_db=True,
    )
    llm_stub.reset()
    # A completion is queued but never consumed: the run raises at require_sandbox BEFORE any
    # model call, so the scripted turn stays untouched.
    llm_stub.script([{"content": "unreached"}])
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(AGENT, {"user_message": uniq("q")}, raise_on_error=False, retry_on_reloading=True)
    assert result.is_error, "a run with no sandbox provider did not fail"
    text = " ".join(getattr(p, "text", "") for p in result.content)
    assert "sandbox" in text.lower(), f"the failure was not the sandbox hard-dependency error: {text}"
    # The run never reached the model — it failed at the sandbox chokepoint before compile.
    assert not llm_stub.requests, f"the run reached the model despite having no sandbox: {llm_stub.requests}"
