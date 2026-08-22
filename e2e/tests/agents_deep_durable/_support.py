"""Shared helpers for the ``langchain_deep_agent`` durable-parity suite.

The suite rides ``deep_agent_durable_stack`` (REPLICAS + redis checkpoint + the fake
persistent sandbox, ``manifests.build_deep_agent_durable_stack``). The deterministic legs
drive the SCRIPTED llm_stub, so they step aside on the real leg — the durable stack repoints
its LLM group at Anthropic when ``claude_agent`` is the selected real seam (``manifests``
§B4), which the scripted turns cannot survive; the one real turn lives in
``test_deep_agent_real_smoke``.

The deep agent's built-in filesystem tools (``write_file`` / ``read_file`` / ``execute``) are
a LIVE durable shell over the ``SandboxSessionBackend`` here, so a scripted model turn that
calls ``write_file`` writes to the persistent workspace VOLUME and a later threaded turn reads
it back — the durability proof the deterministic legs read off ``llm_stub.requests``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from tai42_e2e.manifests import build_deep_agent_durable_stack
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants

# The durable stack replaces the scripted-stub LLM group with real Anthropic when the
# ``claude_agent`` real seam is selected (the ONE real turn is the §5 smoke), so every
# scripted-stub deterministic leg steps aside there — the module stays inert in the default
# mock run (``is_real("claude_agent")`` is False, so collection is byte-for-byte today's).
DETERMINISTIC_MARKS = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("claude_agent"),
        reason="the scripted llm_stub is the mock leg; the durable stack's real LLM turn runs on the creds host",
    ),
]

# The registered agent id (the rename, ``manifests._DEEP_AGENT_ENTRY``). Its run tool binds
# over ``/mcp`` and its SSE run route serves at ``/api/agents/langchain_deep_agent/runs``.
AGENT = "langchain_deep_agent"

# The synthetic execution identity ``e2e_agent_async_ask`` binds and the park stores as its
# ``continuation_identity`` — kept in step with ``_ASYNC_PARK_IDENTITY`` in the probe tools. A
# value the auth-off default execution identity (``None``) never produces, so a resume firing
# under it proves the stored-identity rebind.
STORED_PARK_IDENTITY = "e2e-async-driver"


async def run_sse(stack: TaiStack, port: int, path: str, body: dict[str, Any]) -> httpx.Response:
    """POST an agent run over the SSE run door on ``port`` and return the RAW response.

    The caller inspects ``status_code`` (a 404 for an unknown agent name) or streams
    ``response`` for the ``data:`` frames. The stream is fully drained by the context manager
    exit, so a 200 run completes (and its checkpoint lands) before this returns."""
    url = f"http://{stack.host}:{port}{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request("POST", url, json=body)
    return response


async def run_sse_frames(stack: TaiStack, port: int, path: str, body: dict[str, Any]) -> list[str]:
    """POST an agent run over the SSE run door and return the ``data:`` frames, draining the
    stream so the run completes before returning. Raises on a non-2xx status."""
    url = f"http://{stack.host}:{port}{path}"
    frames: list[str] = []
    async with httpx.AsyncClient(timeout=20.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(line[len("data:") :].strip())
    return frames


def build_deep_durable_no_sandbox_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The durable stack with the ``sandbox_module`` slot EMPTY — a box on which
    ``langchain_deep_agent`` is registered but no sandbox provider is installed.

    Composed off the foundation ``build_deep_agent_durable_stack`` (never a hand-rebuilt
    manifest) with the one scalar sandbox slot dropped, so a run/astream drive raises the
    every-door ``SandboxUnavailableError`` at the ``require_sandbox`` chokepoint (§B3.7) while
    the checkpoint-only ``append_thread_messages`` path — which acquires no session — still
    works (§B3.5). The agent registers regardless: its digest-``session_image`` validation and
    the sandbox dependency both fire at RUN start, never at plugin import."""
    config = build_deep_agent_durable_stack(res, variants)
    manifest = {key: value for key, value in config.manifest.items() if key != "sandbox_module"}
    return replace(config, name="deep-agent-durable-nosbx", manifest=manifest)
