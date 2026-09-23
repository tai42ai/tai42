"""The door ``extras_expr`` and an agent target that DECLARES the extras it reads.

A door (here a conversation agent route) builds a run's extras with ``extras_expr``; the visit
admits only the keys the target declares. An agent declares them through its ``extras_keys`` class
attribute — so a route whose ``extras_expr`` names a declared key reaches the agent run, and one
that names an undeclared key is refused loudly before the run starts.

Over ``agent_route_park_stack`` (a durable agent-state conversation stack, access control ON, the
web channel), driving the caller-ask module's ``e2e_extras_agent`` (declares ``{"tag"}`` and records
the run extras it read):

- ``test_route_extras_expr_reaches_a_declaring_agent`` — a route's ``extras_expr`` of the declared
  key reaches the agent run, which records the extras it read.
- ``test_undeclared_extra_is_refused_at_the_fire`` — a route whose ``extras_expr`` names an
  UNDECLARED key is refused when the run is bound: the agent never starts (no record) and the turn
  delivers the route's client-safe error notice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import overload

import httpx
import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]

_EXTRAS_AGENT = "e2e_extras_agent"


@overload
async def _create_extras_route(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str], *, extras_expr: str
) -> WebChatClient: ...


@overload
async def _create_extras_route(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str], *, extras_expr: str, expect: int
) -> WebChatClient | httpx.Response: ...


async def _create_extras_route(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str], *, extras_expr: str, expect: int = 200
) -> WebChatClient | httpx.Response:
    """Create a web agent route onto the extras agent with ``extras_expr``; return the raw response
    for a refused create (``expect`` other than 200), or the opened web client for an accepted one."""
    stack, root_token = agent_route_park_stack
    api = stack.api(stack.port_b).with_token(root_token)
    identity = uniq("ex-site").replace("_", "-")
    route_name = uniq("ex-route").replace("_", "-")
    execution_key = uniq("ex-exec")
    await api.post(
        "/api/auth/api-keys", json={"user_id": execution_key, "description": "e2e extras key", "scopes": ["e2e-all"]}
    )
    body = {
        "door": "channel",
        "target_kind": "agent",
        "target_name": _EXTRAS_AGENT,
        "execution_key": execution_key,
        "channel": "web",
        "our_identity": identity,
        "extras_expr": {"content": extras_expr},
    }
    if expect != 200:
        return await api.request_raw("POST", f"/api/conversations/{route_name}", json=body)
    await api.post(f"/api/conversations/{route_name}", json=body, expect=200)
    base_url = f"http://{stack.host}:{stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def test_route_extras_expr_reaches_a_declaring_agent(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, _root = agent_route_park_stack
    tag = uniq("ex-tag")
    llm_stub.reset()
    llm_stub.script([{"content": uniq("ex-final")}])

    web = await _create_extras_route(agent_route_park_stack, uniq, extras_expr=f'{{tag: "{tag}"}}')
    assert (await web.send(uniq("ex-msg"))).status_code == 200

    # The agent read the run extras the route's extras_expr built and recorded them under the tag.
    async def _recorded() -> bool | None:
        return True if stack.records(f"agent_extras:{tag}") else None

    await wait_for_async(_recorded, deadline=60.0, message="the declared extras never reached the agent run")


async def test_undeclared_extra_is_refused_at_the_fire(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, _root = agent_route_park_stack
    llm_stub.reset()
    llm_stub.script([{"content": uniq("ex-final")}])

    # ``e2e_extras_agent`` declares only ``{"tag"}``; an ``extras_expr`` naming an undeclared key is
    # refused when the run is bound, so the agent never starts.
    web = await _create_extras_route(agent_route_park_stack, uniq, extras_expr='{"notdeclared": "y"}')
    assert (await web.send(uniq("ex-msg"))).status_code == 200

    # The turn delivers the route's client-safe error notice; the agent never ran (no extras record).
    error_text = "Sorry, something went wrong handling your message. Please try again."
    delivered = await web.frames(
        until=lambda e, d: e == "chat.message" and d["direction"] == "out" and error_text in d["text"],
        deadline=60.0,
    )
    out = [d["text"] for e, d in delivered if e == "chat.message" and d["direction"] == "out"]
    assert any(error_text in t for t in out), f"the undeclared extra was not refused, saw {out!r}"
    assert stack.records("agent_extras:y") == []
