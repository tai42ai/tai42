"""``vqa_agent`` over the stack: one multimodal completion.

The agent builds a single ``HumanMessage`` carrying a text part and an image
part (the image URL normalized through the storage resource manager, which for a
public URL passes it through without a fetch) and streams the model's answer. The
scripted stub answers the completion, and the recorded request proves the image
content-part and the query reached the model."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.netfixtures import TargetServer
from tai42_e2e.stack import TaiStack

# The agents stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = pytest.mark.backendless


async def test_vqa_agent_image_part_reaches_model_over_mcp(
    agents_stack: TaiStack, llm_stub: LlmStub, target_server: TargetServer, uniq: Callable[[str], str]
) -> None:
    # A loopback image URL (image suffix so normalize_media accepts it as an image
    # resource); it is never fetched — the model dereferences it — so the target
    # server need not serve the bytes. Loopback is in the URL-guard CIDR allowlist.
    image_url = f"{target_server.url}/{uniq('vqa')}.png"
    query = f"describe {uniq('subject')}"
    answer = f"a photo of {uniq('ans')}"
    llm_stub.reset()
    llm_stub.script([{"content": answer}])

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool("vqa_agent", {"image_url": image_url, "query": query})

    assert answer in json.dumps(result.data), f"vqa_agent did not return the scripted answer: {result.data}"
    requests = llm_stub.requests
    assert len(requests) == 1, f"expected 1 multimodal completion, saw {len(requests)}"
    # The agent's distinctive behavior is the MULTIMODAL message shape: the image
    # rides as its own ``image_url`` content part, not flattened into the text (which
    # a substring check over the whole body could not tell apart).
    content = requests[0]["messages"][-1]["content"]
    assert isinstance(content, list), f"the model was sent a flat text message, not multimodal parts: {content}"
    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert image_parts, f"the image content-part never reached the model: {content}"
    assert image_url in json.dumps(image_parts), f"the image part carries the wrong url: {image_parts}"
    text_parts = [part for part in content if part.get("type") == "text"]
    assert query in json.dumps(text_parts), f"the text query never reached the model: {text_parts}"
