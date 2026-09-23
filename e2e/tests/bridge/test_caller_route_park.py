"""A caller ask parked from a conversation ROUTE turn, listed once across the turn's subject
candidate union, and resumed by the route's own door contract.

A ``target_kind=tool`` web route drives ``held_route_park``, which async-asks its CALLER: the
caller ask parks on the route turn's subject, indexed under EACH of the turn's candidate keys
(person and thread). A later inbound message fires the route's ``resume_expr`` over ``$parked``
— the run's parked interactions read once, de-duplicated across those candidate keys — so the
single caller ask resumes and its answer is delivered back into the transcript. A doubled
``$parked`` (no de-duplication) would resume the same id twice; the single delivered reply is
the proof it is listed once.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness, wait_probe_record

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted llm_stub is the 'llm' mock leg (bridge LLM env); the real leg on the creds host",
)

# resume_expr resumes each pending caller ask on the turn's subject; a de-duplicated ``$parked``
# yields exactly one resume item for the one parked ask.
_RESUME_ANSWER = "route-resumed"
_RESUME_EXPR = (
    f'[$parked[] | select(.status == "asking" and .to == "caller") | {{id: .id, payload: "{_RESUME_ANSWER}"}}]'
)
# A hook fired on the route subject resumes the parked caller ask; its answer maps through the
# route's own ``reply_expr`` back into the conversation.
_HOOK_RESUME_EXPR = '[$parked[] | select(.status == "asking" and .to == "caller") | {id: .id, payload: "hook-answer"}]'


async def _open_caller_route_visitor(bridge: BridgeHarness, uniq: Callable[[str], str]) -> WebChatClient:
    identity = uniq("crp-site").replace("_", "-")
    route_name = uniq("crp-route").replace("_", "-")
    execution_key = uniq("crp-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    # Multichannel resolves a PERSON for the turn, so its subject carries BOTH a ``thread`` and a
    # ``person`` candidate key — the caller ask indexes under each, and the door's ``$parked`` read
    # unions the two keys, so the de-duplication that lists the one ask once is genuinely exercised.
    await bridge.set_target_config(target_kind="tool", target_name="held_route_park", multichannel=True)
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="held_route_park",
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
        start_expr="{marker: .message}",
        resume_expr=_RESUME_EXPR,
        reply_expr=".answer",
    )
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def test_a_route_parked_caller_ask_is_listed_once_and_resumed_by_the_door(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web = await _open_caller_route_visitor(bridge, uniq)

    # Turn 1: the route drives held_route_park, which asks its caller and parks silently.
    marker = uniq("crp-msg")
    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text
    parked = await wait_probe_record(bridge, f"held_route_park:{marker}", deadline=20.0)
    assert len(parked) == 1, f"expected exactly one park record, got {parked!r}"
    replayed = await web.frames()
    directions = [data["direction"] for event, data in replayed if event == "chat.message"]
    assert directions == ["in"], f"a parked turn must post no reply, saw {directions!r}"

    # Turn 2: the door evaluates resume_expr over $parked (the parked interactions read once across
    # the turn's candidate-key union), resumes the single caller ask, and delivers its answer.
    sent2 = await web.send(uniq("crp-ans"))
    assert sent2.status_code == 200, sent2.text
    delivered = await web.frames(
        until=lambda event, data: (
            event == "chat.message" and data["direction"] == "out" and _RESUME_ANSWER in data["text"]
        ),
        deadline=40.0,
    )
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    # Exactly one reply, carrying the resumed answer: the caller ask was listed once and resumed once.
    replies = [text for text in out_texts if _RESUME_ANSWER in text]
    assert len(replies) == 1, f"the deduplicated caller ask must resume to exactly one reply, saw {out_texts!r}"


async def test_a_route_parked_caller_ask_resumes_from_a_direct_run_on_its_subject(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web = await _open_caller_route_visitor(bridge, uniq)

    # Turn 1: the route parks a caller ask; the park record carries the turn's subject.
    marker = uniq("crp-dr-msg")
    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text
    parked = await wait_probe_record(bridge, f"held_route_park:{marker}", deadline=20.0)
    assert len(parked) == 1, f"expected exactly one park record, got {parked!r}"
    park = json.loads(parked[0]) if isinstance(parked[0], str) else parked[0]
    subject = park["subject"]

    # A DIRECT run on the route subject (addressing its thread candidate) resumes the caller ask
    # a ROUTE turn parked — cross-door: parked by a route, resumed by the tool-runs subject door.
    resume_subject = {
        "target_kind": subject["target_kind"],
        "target_name": subject["target_name"],
        "kind": "thread",
        "key": subject["by_kind"]["thread"],
    }
    api = bridge.api(port=bridge.stack.port_a, token=bridge.root_token)
    submitted = await api.post(
        "/api/tool-runs",
        json={"tool_name": "caller_relay", "arguments": {"payload": "direct-answer"}, "subject": resume_subject},
        expect=202,
        retry_on_reloading=True,
    )
    run_id = submitted["run_id"]

    async def _succeeded() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == "succeeded" else None

    view = await wait_for_async(_succeeded, deadline=20.0, message="the direct-run resume never succeeded")
    outcome = view["result"]
    assert outcome["action"] == "resumed"
    assert outcome["result"]["answer"] == "direct-answer"


async def test_a_route_parked_caller_ask_resumes_from_a_hook_on_its_subject(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web = await _open_caller_route_visitor(bridge, uniq)

    marker = uniq("crp-hook-msg")
    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text
    parked = await wait_probe_record(bridge, f"held_route_park:{marker}", deadline=20.0)
    assert len(parked) == 1, f"expected exactly one park record, got {parked!r}"
    park = json.loads(parked[0]) if isinstance(parked[0], str) else parked[0]
    subject = park["subject"]

    # A HOOK bound to the route subject's thread candidate resumes the caller ask a ROUTE turn
    # parked — a THIRD door. The route pinned the park's delivery binding, so the resumed answer
    # is delivered back into the route's own conversation transcript.
    topic = uniq("crp-hook-topic").replace("_", "-")
    execution_key = uniq("crp-hook-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.api(token=bridge.root_token).post(
        "/api/hooks",
        json={
            "name": f"{topic}-resume",
            "topic": topic,
            "tool": "e2e_echo",
            "start_expr": {"content": "null"},
            "resume_expr": {"content": _HOOK_RESUME_EXPR},
            "execution_key": execution_key,
            "subject": {
                "target_kind": subject["target_kind"],
                "target_name": subject["target_name"],
                "kind": "thread",
                "key_expr": {"content": ".key"},
            },
        },
    )
    await bridge.api(port=bridge.stack.port_a, token=bridge.root_token).request_raw(
        "POST", f"/universal_webhook/{topic}", json={"key": subject["by_kind"]["thread"]}
    )

    # The resumed answer is delivered back into the route's transcript through its bound completion.
    delivered = await web.frames(
        until=lambda event, data: (
            event == "chat.message" and data["direction"] == "out" and "hook-answer" in data["text"]
        ),
        deadline=40.0,
    )
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    assert any("hook-answer" in text for text in out_texts), (
        f"the hook-resumed reply was not delivered, saw {out_texts!r}"
    )
