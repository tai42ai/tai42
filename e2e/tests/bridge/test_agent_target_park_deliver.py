"""A CONVERSATION agent-target route whose AGENT ASYNC-PARKS, is answered out of band, and whose
resumed answer is delivered back into the originating web transcript.

The AGENT-direction mirror of ``test_tool_target_park_deliver``. Both directions of the unified
park-completion binding now have an end-to-end pin, and they exercise DIFFERENT resumers:

* the tool direction's resumer is the parking tool's own continuation, which calls
  ``deliver_tool_completion``;
* this one's resumer is the AGENTS PLUGIN's park driver. Nothing in this spec fires the delivery
  tool — ``agent_resume`` does, from the completion binding the conversation door pinned around
  the agent turn, with the payload the contract pins
  (``{**bound_context, result, completion_id, status}``).

That last point is the whole reason this spec exists. The two packages agree on that payload
across a release boundary, and neither package's own suite can see the pair: an agents plugin that
fires a differently-shaped payload, or a skeleton delivery tool that cannot accept the one it
fires, produces exactly this — a turn that parks, resumes, and silently never delivers, its park
entry retried until it is dropped. Only a spec running the real driver against the real delivery
tool catches it.

Legs:

* a visitor's message parks the agent turn SILENTLY (no synchronous reply, the interaction is
  persisted) — the same silent-turn barrier the tool-target mirror asserts;
* answering the park out of band fires ``agent_resume``, whose completion handoff delivers the
  resumed FINAL ANSWER into the visitor's transcript, where a reconnect replays it;
* the parked tool's ask ran exactly once (the resume substitutes the answer, never re-runs it),
  and the answer is posted exactly once.

There is deliberately no non-success arm here, unlike the tool mirror. The agents driver fires the
completion from its clean-terminal branch ALONE — an errored drive is retried rather than
converted into a failed delivery, because a failure fired under the super-step's stable completion
id would dedupe away the retry's eventual success. There is therefore no agent-direction
non-success fire to assert.

Gated on the module-capable checkpoint Redis: an agent run is park-capable only on a durable
checkpoint provider, so this leg cannot run on the memory-provider bridge profile.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness

# The stack runs no backend worker (every moving part of this leg is in-process), so the module
# is backendless: under a non-default backend variant it would re-run identical work. The scripted
# llm_stub is the LLM MOCK leg — the real-provider leg runs on the e2e creds host.
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]

# The registered park-capable agent the route targets: a ``tools_agent`` with the async-ask probe
# tools baked in (a conversation agent target is invoked with no tool_names, so a bare tools_agent
# route could never reach a parking tool). Kept in step with ``tai42_e2e_fixtures.park_agent``.
_PARK_AGENT = "e2e_park_agent"

# The probe-record key prefix ``e2e_agent_async_ask`` writes its one-per-park record under. The
# parked interaction id is the key's suffix — the id this spec cannot know before the run.
_ASK_RECORD_PREFIX = "agent_async_ask:"

# Far enough out that the answer always resolves the park first — the expiry reaper never races
# this leg.
_PARK_EXPIRY_SECONDS = 3600


def _park_keys(bridge: BridgeHarness) -> set[str]:
    """Every ``e2e_agent_async_ask`` record key currently present."""
    return {key for key in bridge.stack.record_keys() if key.startswith(_ASK_RECORD_PREFIX)}


async def _open_agent_target_web_visitor(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> WebChatClient:
    """Create a ``target_kind=agent`` web route onto the park-capable probe agent and open its
    chat page as a first-time visitor, returning the door client bound to the minted session."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent=_PARK_AGENT,
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
    )
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def _park_a_message(bridge: BridgeHarness, web: WebChatClient, marker: str, before: set[str]) -> str:
    """Send ``marker`` into the visitor's conversation, assert the AGENT turn PARKED SILENTLY, and
    return the parked ``interaction_id``.

    The park barrier is the probe tool's own record: the async ask ran, so the turn reached the
    park. For a silent turn (no outbound send, no readable channel id) that side effect is the
    completion barrier the turn otherwise lacks. ``before`` is the key set captured ahead of the
    send, so the ONE new key names THIS park on a stack shared with other specs."""
    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text

    async def fresh() -> str | None:
        new = _park_keys(bridge) - before
        assert len(new) <= 1, f"more than one park was raised while this spec ran: {new!r}"
        return next(iter(new)) if new else None

    key = await wait_for_async(fresh, deadline=40.0, message="the agent turn never reached its async park")
    interaction_id = key[len(_ASK_RECORD_PREFIX) :]

    # Silent: the parked turn posted nothing back — the transcript holds only the inbound message.
    replayed = await web.frames()
    exchange = [(data["direction"], data["text"]) for event, data in replayed if event == "chat.message"]
    assert [direction for direction, _text in exchange] == ["in"], f"a parked turn must post no reply, saw {exchange!r}"
    assert marker in exchange[0][1]
    return interaction_id


async def test_agent_target_park_delivers_its_resumed_answer_back_to_the_conversation(
    agent_route_bridge: BridgeHarness, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    bridge = agent_route_bridge
    answer = uniq("atdeliver-ans")
    # Turn 1 parks the run on the async ask; the RESUMED turn produces the final answer the
    # completion handoff delivers. (The suite's autouse fixture already reset the stub.)
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": "e2e_agent_async_ask",
                    "arguments": {"question": uniq("atdeliver-q"), "expiry_seconds": _PARK_EXPIRY_SECONDS},
                }
            },
            {"content": answer},
        ]
    )

    web = await _open_agent_target_web_visitor(bridge, uniq, "atdeliver")
    before = _park_keys(bridge)
    marker = uniq("atdeliver-msg")
    interaction_id = await _park_a_message(bridge, web, marker, before)

    # Answer the park out of band through replica A's interactions door. That door claims the
    # answer and fires the AGENTS PLUGIN's ``agent_resume``, which drives the parked graph to its
    # final answer and hands it to ``conversation_deliver`` — the payload contract under test.
    answered = await bridge.api(port=bridge.stack.port_a).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": "resume-me"}
    )
    assert answered["status"] == "answered"

    # The resumed FINAL ANSWER — the agent's own text, not a raw envelope — is delivered back into
    # the visitor's transcript.
    delivered = await web.frames(
        until=lambda event, data: event == "chat.message" and data["direction"] == "out" and answer in data["text"],
        deadline=60.0,
    )
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    # EQUALITY, not containment: the delivered message is the agent's own final text and nothing
    # else. A raw completion envelope (or a serialized result dict) would still CONTAIN the answer
    # while being the exact regression this spec exists to catch.
    assert any(text.strip() == answer for text in out_texts), (
        f"the resumed answer was not delivered as the agent's own text, saw {out_texts!r}"
    )

    # The parked tool's ask ran EXACTLY ONCE — the resume substituted the answer, never re-ran it.
    assert len(bridge.stack.records(f"{_ASK_RECORD_PREFIX}{interaction_id}")) == 1

    # The park resolved exactly once: a second answer through the door is refused, and the
    # transcript still holds ONE copy of the delivered answer.
    late = await bridge.api(port=bridge.stack.port_a).request_raw(
        "POST", f"/api/interactions/{interaction_id}/answer", json={"answer": "resume-me"}
    )
    assert late.status_code == 409, late.text

    replay = await web.frames()
    delivered_replies = [
        data["text"]
        for event, data in replay
        if event == "chat.message" and data["direction"] == "out" and data["text"].strip() == answer
    ]
    assert len(delivered_replies) == 1, f"the resumed answer was posted more than once: {delivered_replies!r}"
