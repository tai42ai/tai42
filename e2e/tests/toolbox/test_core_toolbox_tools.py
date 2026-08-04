"""B4(b) — the four previously-uncovered toolbox tools on the core stack.

``request`` / ``generate_embeddings`` / ``pad_embeddings`` / ``current_time_info`` had zero
e2e (only ``generate_uuid`` was ever loaded). The core profile now carries all four; each is
driven over the server's own MCP surface:

* ``request`` against the harness recording target server (``netfixtures.TargetServer``),
  reached over loopback (the core stack opts the loopback CIDRs into the SSRF guard).
* ``generate_embeddings`` against the LLM stub's ``/v1/embeddings`` via the tool's per-call
  ``base_url`` (``embedding_kwargs``), so no real embedding provider is contacted — the
  boundary PLAN_2 owns.
* ``pad_embeddings`` widening the returned vectors, and ``current_time_info`` returning its
  structured clock — both pure, no backing service.
"""

from __future__ import annotations

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.netfixtures import TargetServer
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# ``test_generate_and_pad_embeddings`` asserts the LLM stub's deterministic /v1/embeddings
# (identical input → identical vectors), so this module carries the embeddings MOCK leg. The
# real-provider embedding boundary is PLAN_2's — exercised on the dedicated e2e creds host
# (PLAN_2 §F), never in CI — so the stub-bound module steps aside when the 'embeddings' seam is
# real. Inert in the default mock run — is_real("embeddings") is False, so collection is
# byte-for-byte today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("embeddings"),
    reason="stub-embeddings determinism is the 'embeddings' mock leg; the real leg on the creds host (PLAN_2 §F)",
)


async def test_request_tool_hits_the_target_server(core_stack: TaiStack, target_server: TargetServer) -> None:
    before = len(target_server.records)
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        result = await mcp.call_tool("request", {"url": f"{target_server.url}/ok"}, retry_on_reloading=True)
    data = result.data
    assert data["status_code"] == 200, data
    assert data["text"] == "target-ok", data
    # The SUT actually dialled the target (recorded in the same process as this test).
    hits = [r for r in target_server.records[before:] if r.path == "/ok"]
    assert len(hits) == 1, target_server.records[before:]


async def test_generate_and_pad_embeddings(core_stack: TaiStack, llm_stub: LlmStub) -> None:
    call_args = {
        "texts": ["alpha", "beta"],
        # The tool's per-call base_url override → the LLM stub's deterministic /v1/embeddings.
        "embedding_kwargs": {"base_url": llm_stub.base_url, "api_key": "e2e-test"},
    }
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        first = (await mcp.call_tool("generate_embeddings", call_args, retry_on_reloading=True)).data
        second = (await mcp.call_tool("generate_embeddings", call_args)).data

    assert isinstance(first, list), first
    assert len(first) == 2, first
    width = len(first[0])
    assert width > 0, first
    assert all(len(vec) == width for vec in first), first
    # The stub is deterministic: identical input yields identical vectors.
    assert first == second, (first, second)

    target_dim = width + 8
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        padded = (await mcp.call_tool("pad_embeddings", {"embeddings": first, "target_dim": target_dim})).data
    assert len(padded) == 2, padded
    assert all(len(vec) == target_dim for vec in padded), padded
    # The leading components are preserved; the tail is zero-padded.
    assert padded[0][:width] == first[0], (padded[0], first[0])
    assert padded[0][width:] == [0.0] * 8, padded[0]


async def test_current_time_info(core_stack: TaiStack) -> None:
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        info = (await mcp.call_tool("current_time_info", retry_on_reloading=True)).data
    assert set(info) >= {"utc", "local", "system"}, info
    assert info["utc"]["year"] >= 2024, info["utc"]
    assert isinstance(info["system"]["epoch_seconds"], (int, float)), info["system"]
