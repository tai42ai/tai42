"""Per-send form data + pages — the composed callback-form-page path.

A channel-delivered ``ask(answer_format="form", data=..., pages=...)`` mints the
callback form page; this exercises the whole seam a participant's traffic takes:

* the GET renders the page with the ``values`` prefilled into their controls, the
  per-send ``options`` as a radio group (short lists) or a ``<select>`` (long lists),
  labels shown and values posted, REPLACING the schema ``enum`` for this send, and the
  ``pages`` as ordered steps (one visible,
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
import pytest
import redis as redis_lib
from fastmcp.client.client import CallToolResult

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:interactions", "setting:tai42_e2e_fixtures.stub_channel", "topology:replicas")

_SCHEMA = {
    "type": "object",
    "required": ["label"],
    "properties": {
        "label": {"type": "string"},
        "color": {"type": "string", "enum": ["red", "blue"]},
        "count": {"type": "integer"},
        "when": {"type": "string", "format": "date"},
    },
}


async def _find_pending(stack: TaiStack, port: int, question: str, *, deadline: float = 8.0) -> dict:
    url = f"{stack.origin(port)}/api/interactions"

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


@pytest.mark.needs("store:redis", "setting:INTERACTIONS_PUBLIC_BASE_URL")
async def test_form_data_pages_render_prefilled_stepped_and_the_post_carries_every_field(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = replicas_stack
    question = uniq("form_dp_q")
    label = uniq("form_dp_label")
    # ``color`` is prefilled to a PER-SEND option value (``green``) that is NOT in the
    # published enum — the send-only option list replaces the enum, end to end.
    data = {
        "values": {"label": label, "color": "green", "when": "2024-01-15"},
        "options": {"color": [{"value": "green", "label": "Green"}, {"value": "amber", "label": "Amber"}]},
    }
    pages = [{"title": "Who", "fields": ["label", "color"]}, {"title": "How many", "fields": ["count", "when"]}]
    answer = {"label": label, "color": "green", "count": 4, "when": "2024-01-15"}

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
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
        return result.data.answer

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
        # ``color`` renders the per-send options as a radio group (two options, below the
        # radio threshold), labels shown and values posted, the prefill checked, and the
        # published enum values do NOT appear.
        assert (
            '<input type="radio" name="color" data-field="color" data-kind="string" value="green" checked> Green'
            in body
        )
        assert '<input type="radio" name="color" data-field="color" data-kind="string" value="amber"> Amber' in body
        assert ">red<" not in body
        assert ">blue<" not in body
        # A ``format: date`` property renders the native date control, prefilled ISO value.
        assert 'data-field="when" data-kind="string" type="date" value="2024-01-15"' in body
        # The pages render as ordered steps: the first visible, the rest hidden, with nav.
        assert '<section class="step" data-step="0" data-page-title="Who">' in body
        assert '<section class="step" data-step="1" data-page-title="How many" hidden>' in body
        assert '<h2 tabindex="-1">Who</h2>' in body
        assert '<h2 tabindex="-1">How many</h2>' in body
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


_SHAPED_SCHEMA = {
    "type": "object",
    "required": ["email"],
    "properties": {
        "email": {"type": "string", "format": "email", "title": "Email address"},
        "qty": {"type": "integer", "minimum": 1},
        "code": {"type": "string", "pattern": "^[0-9]{4}$"},
    },
}


@pytest.mark.needs("store:redis", "setting:INTERACTIONS_PUBLIC_BASE_URL")
async def test_form_shapes_ride_the_page_and_the_door_refuses_with_the_fields_sentence(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = replicas_stack
    question = uniq("form_shape_q")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "stub",
                    "answer_format": "form",
                    "schema": _SHAPED_SCHEMA,
                    "wording": {"form": {"fields": {"email": {"invalid": "Enter your full address"}}}},
                },
                retry_on_reloading=True,
            )
        return result.data.answer

    ask_task = asyncio.create_task(ask())
    try:
        add = await _find_pending(stack, stack.port_b, question)
        ticket = _resolve_ticket(stack, add["interaction_id"])
        api_b = stack.api(port=stack.port_b)

        # The page carries every shape its script checks before the answer leaves it, with the
        # caller's sentence and the platform's per-shape sentences.
        page = await api_b.request_raw("GET", f"/api/interactions/callback/{ticket}")
        assert page.status_code == 200, page.text
        body = page.text
        assert 'data-field="email" data-kind="string" type="email" required' in body
        assert 'data-invalid="Enter your full address"' in body
        assert 'data-msg-format="Enter a valid email address"' in body
        assert 'type="number" step="1" min="1"' in body
        assert 'data-msg-minimum="Enter a number of at least 1"' in body
        assert 'data-pattern="^[0-9]{4}$"' in body

        # The door is the authority: each broken shape is refused with the platform's plain
        # sentence and the field it names (the page pins it under that field), the ask kept.
        for answer, sentence, field in (
            ({"email": "dhdhdh"}, "Enter a valid email address", "email"),
            ({"email": "alice@example.org", "qty": 0}, "Enter a number of at least 1", "qty"),
            ({"email": "alice@example.org", "code": "12a4"}, "This value is not in the expected form", "code"),
            ({}, "This field is required", "email"),
        ):
            refused = await api_b.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": answer})
            assert refused.status_code == 400, refused.text
            assert refused.json()["error"] == sentence
            assert refused.json()["field"] == field
        assert not ask_task.done()

        good = {"email": "alice@example.org", "qty": 2, "code": "1234"}
        posted = await api_b.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": good})
        assert posted.status_code == 200, posted.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()

    assert resolved == good


def _content_text(result: object) -> str:
    """The full text of an MCP result's content blocks. On an error result the ask
    door's refusal message surfaces here (the exception class does not survive MCP
    serialization; the stable message substring does)."""
    blocks = getattr(result, "content", None) or []
    return json.dumps([block.model_dump(mode="json") for block in blocks])


async def _is_pending(stack: TaiStack, port: int, question: str) -> bool:
    url = f"{stack.origin(port)}/api/interactions"
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
            return await mcp.call_tool("ask", arguments, raise_on_error=False, retry_on_reloading=True)

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
            "pages": [{"title": "Only", "fields": ["label", "color", "when"]}],
        }
    )
    assert missing_field.is_error
    assert "count" in _content_text(missing_field)
    assert not await _is_pending(stack, stack.port_b, bad_pages_q)
