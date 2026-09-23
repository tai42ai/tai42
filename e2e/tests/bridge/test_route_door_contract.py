"""The conversation route door contract: the four parkable-door jqs (``start_expr`` /
``cancel_expr`` / ``resume_expr`` / ``extras_expr``) plus ``reply_expr``, driven end to end over a
live route.

Each jq reads the run's currently parked interactions as ``$parked``; ``start_expr`` builds the
started run's kwargs (null starts nothing), ``resume_expr`` resumes/takes a parked interaction,
``cancel_expr`` cancels one, and ``reply_expr`` maps the run's outcome to the participant reply
(``$asks`` the caller-ask entries when the run asked, ``$turn`` the turn ids, ``.`` the finished
result).

The agent leg runs over ``agent_route_bridge`` (durable agent state, the only stack an agent turn
can park on): a route onto the caller-ask probe agent surfaces the run's caller ask through
``reply_expr`` (``$asks``) and resumes it through ``resume_expr`` on the next inbound (``$parked``
bound) — the door-contract round trip. The tool legs run over ``bridge`` and cover a null start
(nothing runs), ``start_expr`` + ``extras_expr`` reaching the run, and ``cancel_expr`` whole-chain
killing a parked run (its FAILED delivered as the route's client-safe notice).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]

# Far enough out that the caller ask stays pending across the two turns — the resume, not the
# expiry reaper, resolves it.
_PARK_EXPIRY_SECONDS = 3600

_DOOR_AGENT = "e2e_door_agent"

# The inbound that cancels the pending caller ask rather than answering it.
_CANCEL_WORD = "cancel"

# The door contract of the caller-ask route. The agent's ``user_message`` comes from the default
# (a route's ``start_expr`` cannot forge the ``TemplatedText`` an agent turn needs), and the
# platform's pending-caller-ask guard already refuses a fresh start while an ask is pending — so
# ``start_expr`` is left unset and the door contract drives the resume:
#  - resume the pending caller ask with the inbound message;
#  - reply the caller ask's question when the run asked, else the finished run's own result.
# ``$parked`` is read by ``resume_expr`` — a resume that names ``$parked[0].id`` proves the run's
# parked interactions are bound.
_RESUME_EXPR = "if ($parked | length) > 0 then {id: $parked[0].id, payload: .message} else null end"
_REPLY_EXPR = "if ($asks | length) > 0 then $asks[0].question else . end"


def _out_carrying(text: str) -> Callable[[str, dict], bool]:
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and text in data["text"]


async def _open_agent_door_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> WebChatClient:
    """Create a web agent route onto the caller-ask probe agent carrying the whole door contract,
    and open its chat page as a first-time visitor."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    body: dict[str, object] = {
        "door": "channel",
        "target_kind": "agent",
        "target_name": _DOOR_AGENT,
        "execution_key": execution_key,
        "channel": "web",
        "our_identity": identity,
        "resume_expr": {"content": _RESUME_EXPR},
        "reply_expr": {"content": _REPLY_EXPR},
    }
    await bridge.api().post(f"/api/conversations/{route_name}", json=body, expect=200)
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def test_agent_caller_ask_surfaces_through_reply_expr_and_resumes_through_resume_expr(
    agent_route_bridge: BridgeHarness, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    bridge = agent_route_bridge
    question = uniq("dc-question")
    answer = uniq("dc-answer")
    # Turn 1 calls the caller-ask tool (the run parks a to="caller" ask); the resumed turn, fed the
    # visitor's reply as the tool answer, produces the final message.
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": "e2e_caller_ask",
                    "arguments": {"question": question, "expiry_seconds": _PARK_EXPIRY_SECONDS},
                }
            },
            {"content": answer},
        ]
    )

    web = await _open_agent_door_route(bridge, uniq, "dcask")

    # Turn 1: nothing parked, so start_expr starts the run; it asks its caller and parks. reply_expr
    # sees $asks non-empty and surfaces the question as the reply.
    sent = await web.send(uniq("dc-open"))
    assert sent.status_code == 200, sent.text
    surfaced = await web.frames(until=_out_carrying(question), deadline=60.0)
    out_texts = [d["text"] for e, d in surfaced if e == "chat.message" and d["direction"] == "out"]
    assert any(question in t for t in out_texts), (
        f"the caller ask was not surfaced through reply_expr, saw {out_texts!r}"
    )

    # Turn 2: the caller ask is $parked, so start_expr yields null (nothing new starts) and
    # resume_expr resumes it with the visitor's message. The run finishes and reply_expr maps its
    # result (`.`) to the reply.
    replied = await web.send(answer + "-reply")
    assert replied.status_code == 200, replied.text
    finished = await web.frames(until=_out_carrying(answer), deadline=60.0)
    final_texts = [d["text"] for e, d in finished if e == "chat.message" and d["direction"] == "out"]
    assert any(answer in t for t in final_texts), f"the resumed run's result was not delivered, saw {final_texts!r}"


async def test_tool_route_null_start_starts_nothing(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    # A tool route whose start_expr always yields null starts no run. reply_expr (`.`) WOULD map a
    # started echo's result to a reply, so the turn's silence proves nothing ran — a discriminating
    # negative, not a bare absence.
    identity = uniq("dcnull-site").replace("_", "-")
    route_name = uniq("dcnull-route").replace("_", "-")
    execution_key = uniq("dcnull-exec")
    marker = uniq("dcnull-marker")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.api().post(
        f"/api/conversations/{route_name}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "e2e_echo",
            "execution_key": execution_key,
            "channel": "web",
            "our_identity": identity,
            "start_expr": {"content": "null"},
            "reply_expr": {"content": "."},
        },
        expect=200,
    )
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text

    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text

    # The transcript settles with only the inbound — a started echo would have posted a reply.
    replay = await web.frames()
    exchange = [(d["direction"], d["text"]) for e, d in replay if e == "chat.message"]
    assert [direction for direction, _t in exchange] == ["in"], f"a null-start turn posted a reply: {exchange!r}"


# The extras value the tool route's ``extras_expr`` builds and the probe echoes back.
_EXTRAS_TAG = "e2e-door-extra"


async def test_tool_route_start_and_extras_expr_reach_the_run(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    # A tool route whose ``start_expr`` maps the inbound to the probe's ``marker`` kwarg and whose
    # ``extras_expr`` builds the run extras. The probe echoes both, and ``reply_expr`` reads the
    # extras tag back — so the reply proves the extras_expr result reached the run.
    identity = uniq("dcextra-site").replace("_", "-")
    route_name = uniq("dcextra-route").replace("_", "-")
    execution_key = uniq("dcextra-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.api().post(
        f"/api/conversations/{route_name}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "e2e_extras_probe",
            "execution_key": execution_key,
            "channel": "web",
            "our_identity": identity,
            "start_expr": {"content": "{marker: .message}"},
            "extras_expr": {"content": f'{{tag: "{_EXTRAS_TAG}"}}'},
            "reply_expr": {"content": ".extras.tag"},
        },
        expect=200,
    )
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text

    sent = await web.send(uniq("dcextra-msg"))
    assert sent.status_code == 200, sent.text
    delivered = await web.frames(until=_out_carrying(_EXTRAS_TAG), deadline=30.0)
    out_texts = [d["text"] for e, d in delivered if e == "chat.message" and d["direction"] == "out"]
    assert any(_EXTRAS_TAG in t for t in out_texts), f"the extras tag did not reach the run, saw {out_texts!r}"


async def test_tool_route_cancel_expr_kills_a_parked_run(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    # A tool route whose target parks (capturing the door's completion binding). The first inbound
    # parks it silently; the cancel inbound's cancel_expr names $parked[0].id, whole-chain killing
    # the run — the door delivers the run's single FAILED as the route's client-safe error notice.
    from ._bridge_support import ERROR_ANSWER_TEXT, wait_probe_record

    identity = uniq("dccancel-site").replace("_", "-")
    route_name = uniq("dccancel-route").replace("_", "-")
    execution_key = uniq("dccancel-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.api().post(
        f"/api/conversations/{route_name}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "e2e_tool_target_park",
            "execution_key": execution_key,
            "channel": "web",
            "our_identity": identity,
            # Park a run only when nothing is parked; the cancel inbound then targets $parked.
            "start_expr": {"content": "if ($parked | length) > 0 then null else {marker: .message} end"},
            "cancel_expr": {
                "content": (
                    f'if .message == "{_CANCEL_WORD}" and ($parked | length) > 0 then $parked[0].id else null end'
                )
            },
            "reply_expr": {"content": ".result.answer"},
        },
        expect=200,
    )
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text

    # Turn 1 parks silently; the probe records the parked interaction under its marker.
    marker = uniq("dccancel-marker")
    assert (await web.send(marker)).status_code == 200
    parked = await wait_probe_record(bridge, f"tool_target_park:{marker}", deadline=20.0)
    assert len(parked) == 1, parked

    # Turn 2: cancel_expr names the parked id; the whole-chain kill delivers the run's FAILED as the
    # route's uniform client-safe notice.
    assert (await web.send(_CANCEL_WORD)).status_code == 200
    delivered = await web.frames(until=_out_carrying(ERROR_ANSWER_TEXT), deadline=40.0)
    out_texts = [d["text"] for e, d in delivered if e == "chat.message" and d["direction"] == "out"]
    assert any(ERROR_ANSWER_TEXT in t for t in out_texts), (
        f"the cancel did not deliver the FAILED notice, saw {out_texts!r}"
    )
