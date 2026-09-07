"""Per-send form data + pages — the composed callback-form-page path.

A channel-delivered ``ask_user(answer_format="form", data=..., pages=...)`` mints the
callback form page; this exercises the whole seam a guest's traffic takes:

* the GET renders the page with the ``values`` prefilled into their controls, the
  per-send ``options`` as a ``<select>`` (labels shown, values posted) that REPLACES
  the schema ``enum`` for this send, and the ``pages`` as ordered steps (one visible,
  the rest hidden, with Back/Next/Submit nav);
* the POST of the union of every step's fields resolves the ask with the typed dict —
  including a per-send option value that is NOT in the published enum;
* a bad ``values`` key and a page that omits a property are each refused at the ask
  door, naming the offending field, before any state is written (nothing pending).

The generic ``stub`` channel (advertising form delivery, mounting no route) is the
channel-agnostic vehicle: the core renders the same page for every channel that opens
it, so this proves the platform surface without naming any medium.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import redis as redis_lib
from fastmcp.client.client import CallToolResult

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

_SCHEMA = {
    "type": "object",
    "required": ["label"],
    "properties": {
        "label": {"type": "string"},
        "color": {"type": "string", "enum": ["red", "blue"]},
        "count": {"type": "integer"},
    },
}


async def _find_pending(stack: TaiStack, port: int, question: str, *, deadline: float = 8.0) -> dict:
    url = f"http://{stack.host}:{port}/api/interactions"

    async def probe() -> dict | None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            try:
                resp = await client.get(url, params={"page": 1, "pageSize": 200})
                resp.raise_for_status()
            except httpx.HTTPError:
                return None
            for item in resp.json()["data"]["items"]:
                if question in json.dumps(item):
                    return item
        return None

    found = await wait_for_async(
        probe, deadline=deadline, message=f"pending interaction for {question!r} never appeared"
    )
    assert found is not None
    return found


def _resolve_ticket(stack: TaiStack, interaction_id: str) -> str:
    """Recover the callback ticket for ``interaction_id`` from Redis (a bearer
    capability the admin API never serializes)."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        for key in client.scan_iter(match=f"{stack.resources.bus_namespace}:interactions:ticket:*"):
            if client.get(key) == interaction_id:
                return key.rsplit(":", 1)[-1]
    finally:
        client.close()
    raise AssertionError(f"no callback ticket in Redis for interaction {interaction_id}")


async def test_form_data_pages_render_prefilled_stepped_and_the_post_carries_every_field(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = replicas_stack
    question = uniq("form_dp_q")
    label = uniq("form_dp_label")
    # ``color`` is prefilled to a PER-SEND option value (``green``) that is NOT in the
    # published enum — the send-only option list replaces the enum, end to end.
    data = {
        "values": {"label": label, "color": "green"},
        "options": {"color": [{"value": "green", "label": "Green"}, {"value": "amber", "label": "Amber"}]},
    }
    pages = [{"title": "Who", "fields": ["label", "color"]}, {"title": "How many", "fields": ["count"]}]
    answer = {"label": label, "color": "green", "count": 4}

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {
                    "question": question,
                    "channel": "stub",
                    "answer_format": "form",
                    "schema": _SCHEMA,
                    "data": data,
                    "pages": pages,
                },
                retry_on_reloading=True,
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        add = await _find_pending(stack, stack.port_b, question)
        interaction_id = add["interaction_id"]
        ticket = _resolve_ticket(stack, interaction_id)
        api_b = stack.api(port=stack.port_b)

        # GET renders the schema-driven page (a GET never mutates state).
        page = await api_b.request_raw("GET", f"/api/interactions/callback/{ticket}")
        assert page.status_code == 200, page.text
        body = page.text
        # ``label`` is prefilled into its text control.
        assert f'data-field="label" data-kind="string" type="text" value="{label}"' in body
        # ``color`` renders the per-send options (labels shown, values posted), the prefill
        # selected, and the published enum values do NOT appear.
        assert '<option value="green" selected>Green</option>' in body
        assert '<option value="amber">Amber</option>' in body
        assert ">red<" not in body
        assert ">blue<" not in body
        # The pages render as ordered steps: the first visible, the rest hidden, with nav.
        assert '<section class="step" data-step="0">' in body
        assert '<section class="step" data-step="1" hidden>' in body
        assert "<h2>Who</h2>" in body
        assert "<h2>How many</h2>" in body
        assert 'data-nav="back"' in body
        assert 'data-nav="next"' in body
        assert 'data-nav="submit"' in body

        # The POST of the union of every step's fields (including the per-send option value
        # outside the published enum) resolves the ask with the typed dict.
        posted = await api_b.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": answer})
        assert posted.status_code == 200, posted.text
        assert posted.json()["data"]["status"] == "answered"
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()

    assert resolved == answer


def _content_text(result: object) -> str:
    """The full text of an MCP result's content blocks. On an error result the ask
    door's refusal message surfaces here (the exception class does not survive MCP
    serialization; the stable message substring does)."""
    blocks = getattr(result, "content", None) or []
    return json.dumps([block.model_dump(mode="json") for block in blocks])


async def _is_pending(stack: TaiStack, port: int, question: str) -> bool:
    url = f"http://{stack.host}:{port}/api/interactions"
    async with httpx.AsyncClient(timeout=2.0) as client:
        resp = await client.get(url, params={"page": 1, "pageSize": 200})
        resp.raise_for_status()
        return any(question in json.dumps(item) for item in resp.json()["data"]["items"])


async def test_bad_form_data_and_pages_are_refused_at_the_ask_door_before_any_state(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = replicas_stack

    async def ask(arguments: dict) -> CallToolResult:
        async with stack.mcp(port=stack.port_a) as mcp:
            return await mcp.call_tool("ask_user", arguments, raise_on_error=False, retry_on_reloading=True)

    # A ``values`` key naming a property the schema does not declare is refused at the ask
    # door, naming the field, BEFORE any state is written (nothing pending).
    bad_values_q = uniq("bad_values_q")
    bad_values = await ask(
        {
            "question": bad_values_q,
            "channel": "stub",
            "answer_format": "form",
            "schema": _SCHEMA,
            "data": {"values": {"ghost": "x"}},
        }
    )
    assert bad_values.is_error
    assert "ghost" in _content_text(bad_values)
    assert not await _is_pending(stack, stack.port_b, bad_values_q)

    # A ``pages`` layout that omits a declared property is refused the same way, naming it.
    bad_pages_q = uniq("bad_pages_q")
    missing_field = await ask(
        {
            "question": bad_pages_q,
            "channel": "stub",
            "answer_format": "form",
            "schema": _SCHEMA,
            "pages": [{"title": "Only", "fields": ["label", "color"]}],
        }
    )
    assert missing_field.is_error
    assert "count" in _content_text(missing_field)
    assert not await _is_pending(stack, stack.port_b, bad_pages_q)
