"""Shared helpers for the ``claude_code`` deterministic e2e suite.

Every deterministic leg boots a fresh ``build_claude_agent_stack`` SUT through the
function-scoped ``fresh_stack`` factory, overriding ``SANDBOX_FAKE_RUNNER`` to select the
scripted runner script for the turn (the runner-selection seam the fake sandbox provider
reads at exec time). A per-test boot is what lets each leg pin its own script — the fake
provider reads ``SANDBOX_FAKE_RUNNER`` from the SUT process env, fixed at boot.

The claude turn is reached over the two identity-less, ephemeral doors the stack exposes:
the ``/mcp`` tool face (``mcp.call_tool("claude_code", ...)``) and the SSE run route
(``POST /api/agents/claude_code/runs``). ``claude_code`` refuses a caller-supplied
``thread_id`` (workspace identity is never derivable from unauthenticated input), so every
e2e turn here is a fresh ephemeral workspace — the threaded/identity-bound conversation-bridge
door is not wired on this stack (see the module docstrings that skip those legs).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_claude_agent_stack
from tai42_e2e.stack import TaiStack

# The SSE run route for the registered ``claude_code`` agent (the raw, identity-less,
# ephemeral door — the tool-face's HTTP twin).
CLAUDE_RUN_PATH = "/api/agents/claude_code/runs"

# The fake session-image digest the stack pins; a bare tag is rejected at run start. A test
# overrides ``TAI_AGENTS_CLAUDE_SESSION_IMAGE`` to exercise the digest-only rule.
STUB_DIGEST = "registry.example/claude@sha256:" + "c" * 64


def claude_stack(
    fresh_stack: Callable[..., TaiStack],
    llm_stub: LlmStub,
    script: str,
    **env: str,
) -> TaiStack:
    """Boot a fresh ``claude_code`` SUT wired to the scripted runner ``script``.

    ``env`` overlays extra process env (auth-mode toggles, the session-image, the creds
    list, the turn budget) onto the builder's env — the ``fresh_stack`` overlay wins, so a
    leg pins exactly the operator config it drives. The scripted runner replaces the vendor
    SDK, so no ``ANTHROPIC_API_KEY`` and no network are ever needed."""
    return fresh_stack(
        build_claude_agent_stack,
        env_overrides={"SANDBOX_FAKE_RUNNER": f"stub:{script}", **env},
        resource_kwargs={"llm_base_url": llm_stub.base_url},
    )


async def run_sse(stack: TaiStack, body: dict, *, path: str = CLAUDE_RUN_PATH) -> list[dict]:
    """Drive the SSE run route and return every ``data:`` frame parsed as JSON.

    Each frame is one ``StreamEvent`` (``{"type": "message_final", ...}`` etc.), a terminal
    ``{"type": "stream.end"}``, or a loud ``{"type": "stream.error", "message": ...}`` — the
    adapter surfaces a protocol/budget/identity fault as the error frame, never a silent
    close."""
    url = f"http://{stack.host}:{stack.port_a}{path}"
    frames: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
    return frames


def frames_of_type(frames: list[dict], type_name: str) -> list[dict]:
    """Every frame whose discriminator ``type`` equals ``type_name``."""
    return [frame for frame in frames if frame.get("type") == type_name]


def error_text(result: object) -> str:
    """The text of an ``is_error`` MCP tool result (a ``CallToolResult``)."""
    content = getattr(result, "content", None) or []
    return next((getattr(part, "text", "") for part in content), "")
