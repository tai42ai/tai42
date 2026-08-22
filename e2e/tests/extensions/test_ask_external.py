"""The ``ask_external`` builtin tool extension (TRANSFORMER) through the
stack.

Distinct from the interactions suite's ``ask_user`` external-format spec: that one
calls the ``ask_user`` tool directly with a literal ``link`` template. THIS one
drives the EXTENSION, which composes a tool into the external ask — it presents
the wrapped tool's params minus ``callback_url`` plus ``question`` /
``answer_schema`` / ``timeout``, calls the wrapped tool with the platform-minted
``callback_url`` to BUILD the external link, and blocks until the signed callback
delivers the answer.

Observable effects asserted: (1) the branch tool's schema is the composed one
(``callback_url`` hidden, the control params injected); (2) calling it opens a
pending external interaction whose link is the one the WRAPPED TOOL built from the
callback url; (3) answering through the public callback door wakes the blocked
call with that answer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def _find_pending(stack: TaiStack, question: str, *, deadline: float = 10.0) -> dict:
    """Poll the paged pending-list door until the pending interaction carrying
    ``question`` appears; return its payload."""
    url = f"http://{stack.host}:{stack.port_a}/api/interactions"

    async def probe() -> dict | None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            try:
                resp = await client.get(url, params={"page": 1, "pageSize": 200})
                resp.raise_for_status()
            except httpx.HTTPError:
                return None
            for item in resp.json()["data"]["items"]:
                if isinstance(item, dict) and question in json.dumps(item):
                    return item
        return None

    found = await wait_for_async(probe, deadline=deadline, message=f"no pending external interaction for {question!r}")
    assert found is not None
    return found


def _resolve_ticket(stack: TaiStack, interaction_id: str) -> str:
    """Recover the callback ticket for ``interaction_id`` from Redis (the ticket is
    a bearer capability the admin API never serializes)."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        for key in client.scan_iter(match="interactions:ticket:*"):
            if client.get(key) == interaction_id:
                return key.rsplit(":", 1)[-1]
    finally:
        client.close()
    raise AssertionError(f"no callback ticket in Redis for interaction {interaction_id}")


async def test_ask_external_branch_presents_the_composed_schema(extensions_stack: TaiStack) -> None:
    async with extensions_stack.mcp() as mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "e2e_external_link_ask_external" in tools, f"ask_external branch not served: {sorted(tools)}"
    properties = tools["e2e_external_link_ask_external"].inputSchema.get("properties", {})
    # The platform supplies callback_url, so it is hidden from the caller; the three
    # control params are injected in its place.
    assert "callback_url" not in properties, f"ask_external leaked callback_url to the caller: {properties}"
    assert {"question", "answer_schema", "timeout"} <= set(properties), (
        f"ask_external did not inject its control params: {properties}"
    )


async def test_ask_external_opens_the_wrapped_tools_link_and_wakes_on_callback(
    extensions_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")

    async def ask() -> object:
        async with extensions_stack.mcp() as mcp:
            result = await mcp.call_tool("e2e_external_link_ask_external", {"question": question})
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        pending = await _find_pending(extensions_stack, question)
        interaction_id = pending.get("interaction_id") or pending.get("id")
        assert isinstance(interaction_id, str)

        ticket = _resolve_ticket(extensions_stack, interaction_id)

        # The pending interaction carries the link the WRAPPED TOOL built from the
        # platform-minted callback url — the extension called e2e_external_link with
        # callback_url and used its return value as the external link.
        blob = json.dumps(pending)
        assert "https://ext.example/act?cb=" in blob, f"the wrapped tool's link is not on the interaction: {blob}"
        assert ticket in blob, f"the built link does not embed the callback ticket: {blob}"

        api = extensions_stack.api()
        response = await api.request_raw(
            "POST", f"/api/interactions/callback/{ticket}", json={"answer": "answered-externally"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "answered"

        answer = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()

    # The blocked branch call woke with the answer delivered through the callback.
    assert "answered-externally" in json.dumps(answer), f"ask_external did not return the callback answer: {answer}"
