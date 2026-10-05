"""``response_format`` against the REAL configured provider (creds host only).

On the real ``llm`` leg (``REAL_E2E_LLM_PROVIDER`` / ``REAL_E2E_LLM_MODEL``), a tools_agent
``response_format`` run over the ``TurnIntake``-shaped schema (nested objects, nullable type
arrays) yields a structured verdict. This is the test that reproduced the operator's shown
failure when the configured model id refuses forced tool choice — the native plan now carries
it. It is skipped on the mock leg (the scripted stub leg covers the wiring there); it never
runs in CI.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    # The inverse of the mock-leg module: this runs ONLY against a real provider, on the e2e
    # creds host — never in the default mock CI run.
    pytest.mark.skipif(
        not HarnessSettings().is_real("llm"),
        reason="real-provider structured-output leg; runs on the e2e creds host, not the mock CI leg",
    ),
    pytest.mark.needs("helper:llm", "setting:agent:tools_agent"),
]

_TURN_INTAKE_SCHEMA: dict[str, Any] = {
    "title": "TurnIntake",
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "priority": {"type": ["integer", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "detail": {
            "type": "object",
            "title": "Detail",
            "properties": {"note": {"type": ["string", "null"]}},
        },
    },
    "required": ["summary"],
}


async def _run_sse(stack: TaiStack, path: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{stack.origin(stack.port_a)}{path}"
    frames: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
    return frames


async def test_real_provider_yields_a_structured_verdict(agents_stack: TaiStack) -> None:
    frames = await _run_sse(
        agents_stack,
        "/api/agents/tools_agent/runs",
        {
            "user_message": {"content": "Summarise: a user asked to reset their password."},
            "response_format": _TURN_INTAKE_SCHEMA,
        },
    )
    types = [frame.get("type") for frame in frames]
    assert "stream.error" not in types, f"the real structured run errored: {frames}"
    finals = [frame for frame in frames if frame.get("type") == "structured_final"]
    assert len(finals) == 1, f"expected exactly one structured_final: {frames}"
    data = finals[0]["data"]
    assert isinstance(data, dict)
    assert isinstance(data.get("summary"), str)
    assert data["summary"]
    assert types[-1] == "stream.end", f"the stream did not terminate cleanly: {frames}"
