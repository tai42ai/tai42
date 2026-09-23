"""Killing a parked caller ask, on the subject door and through the conversation route doors.

A parked ask is torn down several ways, and every way whole-chain-kills the run — leaving no
driver state and delivering the run's single FAILED to its door exactly once.

On the tool-runs SUBJECT door (``caller_stack``, expiry reaper pinned to 1s, deadline forced past
through the ``e2e_expire_park`` probe):

* ``test_expiry_kills_the_whole_chain`` — an unanswered ``on_expiry="kill"`` ask reaped past its
  deadline leaves nothing on the subject;
* ``test_on_expiry_resume_fires_the_continuation_instead_of_killing`` — ``on_expiry="resume"``
  fires the stored continuation with the expiry marker rather than killing;
* ``test_cancel_tears_down_the_chain`` — an explicit ``cancel_parked`` kills the chain the same way.

Through the conversation route DOORS (the bridge builders — a route parks a caller ask, then a
management door kills it), covering both driver kinds the teardown must reach: the FIXTURE caller
driver (``held_route_park``, whose state is the platform interaction store) and the AGENTS plugin
driver (an agent target's tool asks its caller, whose state is the plugin's own durable park
index):

* ``test_thread_delete_tears_down_a_route_parked_caller_ask`` /
  ``test_route_delete_tears_down_a_route_parked_caller_ask`` /
  ``test_person_erase_tears_down_a_route_parked_caller_ask`` — deleting the thread, the route, or
  the person the fixture-driver caller ask parked under leaves no ask on the subject and delivers
  the door its single FAILED notice;
* ``test_thread_delete_leaves_no_agents_driver_state`` — the same thread-delete kill of an
  AGENT-driven route-parked caller ask leaves no ask on the subject (the agents plugin's park index
  is torn down too), never a resumable orphan;
* ``test_a_route_started_user_ask_killed_by_expiry_delivers_one_door_failed`` — a route-started
  ``to="user"`` ask reaped past its deadline delivers the run's single FAILED to the door ONCE;
* ``test_an_agent_route_started_user_ask_killed_by_expiry_delivers_one_door_failed`` — the AGENT
  twin: an agent turn parks a ``to="user"`` ask out of band, and its expiry kill delivers the run's
  single FAILED to the door ONCE (through the agents plugin's own park index and chain);
* ``test_a_kill_at_a_nested_caller_ask_delivers_one_door_failed_keyed_by_completion`` — a kill
  entering at a NESTED ``to="caller"`` ask of a route-started agent chain reads the run's stored
  delivery address and delivers the SAME door FAILED once, keyed by the run's completion.

Every leg's kill is in-process (the reaper and the delivery machine are plain asyncio tasks, no
backend worker), so the module is ``backendless``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.webchat import WebChatClient

from ._caller_support import await_result, await_status, caller_ask_id, subject, submit

pytestmark = pytest.mark.backendless

# The client-safe text a killed run's door FAILED delivers back into the transcript (mirrors the
# turn engine's own constant); a route-door kill leg matches the delivered notice against it.
_ERROR_ANSWER_TEXT = "Sorry, something went wrong handling your message. Please try again."

# Far enough out that a route-parked ask stays pending until the kill under test resolves it — the
# expiry legs force the deadline past explicitly rather than waiting on this window.
_ROUTE_PARK_EXPIRY_SECONDS = 3600

# Skip the route-door legs on the real-LLM leg: their agent turns run on the scripted stub.
_REAL_LLM = HarnessSettings().is_real("llm")


async def _expire(stack: TaiStack, interaction_id: str) -> None:
    """Force a live park's deadline into the past so the 1s reaper resolves it next pass."""
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.call_tool("e2e_expire_park", {"interaction_id": interaction_id}, retry_on_reloading=True)


async def _await_subject_cleared(stack: TaiStack, subj: dict[str, str], *, deadline: float = 15.0) -> None:
    """Poll until the subject holds no caller ask — the whole-chain teardown is asynchronous."""

    async def _cleared() -> bool | None:
        run_id = await submit(stack.api(port=stack.port_b), "caller_list", {}, subj)
        entries = await await_result(stack.api(port=stack.port_a), run_id)
        return True if entries == [] else None

    await wait_for_async(_cleared, deadline=deadline, message="the killed caller ask never left the subject")


async def test_expiry_kills_the_whole_chain(caller_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")
    interaction_id = await caller_ask_id(api_a, subj)

    # Force the deadline past: under the default kill policy the reaper tears the whole chain
    # down, so nothing survives on the subject.
    await _expire(caller_stack, interaction_id)
    await _await_subject_cleared(caller_stack, subj)


async def test_on_expiry_resume_fires_the_continuation_instead_of_killing(
    caller_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q"), "on_expiry": "resume"}, subj)
    await await_status(api_a, run_id, "parked")
    interaction_id = await caller_ask_id(api_a, subj)

    # The reaper fires the stored continuation with the expiry marker rather than killing:
    # held_run_resume runs and records the drive on the probe channel.
    await _expire(caller_stack, interaction_id)

    async def _resumed() -> dict | None:
        records = caller_stack.records(f"held_run:{interaction_id}")
        if not records:
            return None
        assert len(records) == 1, f"the continuation fired more than once: {records}"
        return json.loads(records[0])

    record = await wait_for_async(
        _resumed, deadline=15.0, message="the expired caller ask never fired its resume continuation"
    )
    # The drive ran the restored chain (held_run) — the continuation tool's own name absent.
    assert record["chain"] == ["held_run"]


async def test_cancel_tears_down_the_chain(caller_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = caller_stack.api(port=caller_stack.port_a)
    api_b = caller_stack.api(port=caller_stack.port_b)
    subj = subject(uniq("subject"))

    run_id = await submit(api_a, "held_run", {"marker": uniq("q")}, subj)
    await await_status(api_a, run_id, "parked")

    # A later run on the subject cancels the caller ask through the generic cancel tool: the
    # visit names the one id it tore down, and nothing survives on the subject.
    cancel_run_id = await submit(api_b, "caller_cancel", {}, subj)
    cancel_outcome = await await_result(api_a, cancel_run_id)
    assert len(cancel_outcome["cancelled"]) == 1
    await _await_subject_cleared(caller_stack, subj)


# ---- route-door kills: a caller ask parked from a conversation route, torn down by a door -------


def _api(stack: TaiStack, token: str) -> ApiClient:
    """The seeded-root admin client for the route-management doors."""
    return stack.api(port=stack.port_b).with_token(token)


async def _mint_key(api: ApiClient, user_id: str) -> None:
    await api.post(
        "/api/auth/api-keys", json={"user_id": user_id, "description": "e2e kill key", "scopes": ["e2e-all"]}
    )


async def _list_caller_asks_on(stack: TaiStack, token: str, subj: dict[str, str]) -> list[dict[str, Any]]:
    """Submit ``caller_list`` on ``subj`` through the tool-runs subject door and return its entries —
    the caller asks the platform interaction store still holds under the route turn's subject."""
    api = _api(stack, token)
    run_id = await submit(api, "caller_list", {}, subj)
    return await await_result(api, run_id)


async def _await_route_subject_cleared(
    stack: TaiStack, token: str, subj: dict[str, str], *, deadline: float = 20.0
) -> None:
    """Poll until the subject holds no caller ask — the whole-chain teardown is asynchronous."""

    async def _cleared() -> bool | None:
        return True if await _list_caller_asks_on(stack, token, subj) == [] else None

    await wait_for_async(_cleared, deadline=deadline, message="the killed caller ask never left the subject")


def _thread_subject(park_subject: dict[str, Any]) -> dict[str, str]:
    """The tool-runs subject that addresses the route turn's THREAD candidate — the scope a later
    subject-door run reads the route-parked caller ask back on."""
    return {
        "target_kind": park_subject["target_kind"],
        "target_name": park_subject["target_name"],
        "kind": "thread",
        "key": park_subject["by_kind"]["thread"],
    }


async def _open_held_route_and_park(
    stack: TaiStack, token: str, uniq: Callable[[str], str], *, tag: str, multichannel: bool = False
) -> tuple[WebChatClient, str, dict[str, Any]]:
    """Create a web tool route onto ``held_route_park``, open its chat page, and drive one turn that
    parks a ``to="caller"`` ask silently. Returns the web client, the route name, and the park record
    (which carries the interaction id and the turn's subject)."""
    api = _api(stack, token)
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await _mint_key(api, execution_key)
    if multichannel:
        # Multichannel resolves a PERSON for the turn, so its subject carries a person candidate the
        # person-erase door addresses.
        await api.put(
            "/api/conversation-configs/tool/held_route_park",
            json={"multichannel": True, "greeting_template": None},
            expect=200,
        )
    await api.post(
        f"/api/conversations/{route_name}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "held_route_park",
            "execution_key": execution_key,
            "channel": "web",
            "our_identity": identity,
            "start_expr": {"content": "{marker: .message}"},
            "reply_expr": {"content": ".answer"},
        },
        expect=200,
    )
    base_url = f"http://{stack.host}:{stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=stack.resources.redis_url)
    assert page.status_code == 200, page.text

    marker = uniq(f"{tag}-msg")
    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text

    async def _parked() -> dict[str, Any] | None:
        records = stack.records(f"held_route_park:{marker}")
        return json.loads(records[0]) if records else None

    park = await wait_for_async(_parked, deadline=25.0, message="the route turn never parked a caller ask")
    return web, route_name, park


async def _assert_door_failed_once(web: WebChatClient, *, deadline: float = 40.0) -> None:
    """Wait until the killed run's client-safe FAILED notice is delivered into the transcript, and
    assert it is delivered EXACTLY ONCE — the run's single door FAILED, never one per interaction."""
    delivered = await web.frames(
        until=lambda event, data: (
            event == "chat.message" and data["direction"] == "out" and _ERROR_ANSWER_TEXT in data["text"]
        ),
        deadline=deadline,
    )
    notices = [
        data["text"]
        for event, data in delivered
        if event == "chat.message" and data["direction"] == "out" and _ERROR_ANSWER_TEXT in data["text"]
    ]
    assert len(notices) == 1, f"the door FAILED must be delivered exactly once, saw {notices!r}"


async def test_thread_delete_tears_down_a_route_parked_caller_ask(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    web, route_name, park = await _open_held_route_and_park(stack, token, uniq, tag="ktd")
    subj = park["subject"]
    thread_id = subj["by_kind"]["thread"]

    # Forget the thread the ask parked under: the door whole-chain-kills every ask on it.
    from urllib.parse import urlencode

    await _api(stack, token).delete(f"/api/conversations/{route_name}/thread?{urlencode({'thread_id': thread_id})}")

    await _await_route_subject_cleared(stack, token, _thread_subject(subj))
    await _assert_door_failed_once(web)


async def test_route_delete_tears_down_a_route_parked_caller_ask(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    web, route_name, park = await _open_held_route_and_park(stack, token, uniq, tag="krd")
    subj = park["subject"]

    # Delete the whole route: the door whole-chain-kills every ask on each of its threads.
    await _api(stack, token).delete(f"/api/conversations/{route_name}")

    await _await_route_subject_cleared(stack, token, _thread_subject(subj))
    await _assert_door_failed_once(web)


async def test_person_erase_tears_down_a_route_parked_caller_ask(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    web, _route_name, park = await _open_held_route_and_park(stack, token, uniq, tag="kpe", multichannel=True)
    subj = park["subject"]
    person_id = subj["by_kind"]["person"]

    # Erase the person the ask parked under: the door whole-chain-kills across the aggregated thread
    # AND the person subject.
    await _api(stack, token).delete(f"/api/conversations/persons/{person_id}")

    await _await_route_subject_cleared(stack, token, _thread_subject(subj))
    await _assert_door_failed_once(web)


# ---- route-door kills of an AGENT-driven caller ask, and a route-started user ask on expiry -----

_DOOR_AGENT = "e2e_door_agent"
_CANCEL_WORD = "cancel"
# The caller-ask door contract: surface the pending ask's question when the run asked, resume it
# with the inbound otherwise, and cancel the named parked id on the cancel word.
_ASK_REPLY_EXPR = "if ($asks | length) > 0 then $asks[0].question else . end"
_ASK_RESUME_EXPR = "if ($parked | length) > 0 then {id: $parked[0].id, payload: .message} else null end"
_ASK_CANCEL_EXPR = f'if .message == "{_CANCEL_WORD}" and ($parked | length) > 0 then $parked[0].id else null end'


async def _open_agent_route(
    stack: TaiStack,
    token: str,
    uniq: Callable[[str], str],
    *,
    agent: str,
    tag: str,
    reply_expr: str | None = None,
    resume_expr: str | None = None,
    cancel_expr: str | None = None,
) -> tuple[WebChatClient, str]:
    """Create a web AGENT route onto ``agent`` carrying the given door contract, open its chat page."""
    api = _api(stack, token)
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await _mint_key(api, execution_key)
    body: dict[str, Any] = {
        "door": "channel",
        "target_kind": "agent",
        "target_name": agent,
        "execution_key": execution_key,
        "channel": "web",
        "our_identity": identity,
    }
    if reply_expr is not None:
        body["reply_expr"] = {"content": reply_expr}
    if resume_expr is not None:
        body["resume_expr"] = {"content": resume_expr}
    if cancel_expr is not None:
        body["cancel_expr"] = {"content": cancel_expr}
    await api.post(f"/api/conversations/{route_name}", json=body, expect=200)
    base_url = f"http://{stack.host}:{stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web, route_name


def _out_carrying(text: str) -> Callable[[str, dict], bool]:
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and text in data["text"]


async def _await_route_thread(api: ApiClient, route_name: str, *, deadline: float = 20.0) -> str:
    """Poll the route's thread listing until its single thread appears; return the thread id."""

    async def _one() -> str | None:
        listing = await api.get(f"/api/conversations/{route_name}/threads")
        items = listing["items"]
        return items[0]["thread_id"] if len(items) == 1 else None

    return await wait_for_async(_one, deadline=deadline, message=f"route {route_name} never showed its thread")


@pytest.mark.skipif(_REAL_LLM, reason="the agent turn runs on the scripted llm_stub; the real leg is on the creds host")
async def test_thread_delete_leaves_no_agents_driver_state(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    question = uniq("ad-q")
    # Turn 1 calls the caller-ask tool; the agent run parks a to="caller" ask in the AGENTS plugin's
    # own park index. No second turn — the kill, not an answer, resolves it.
    llm_stub.reset()
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": "e2e_caller_ask",
                    "arguments": {"question": question, "expiry_seconds": _ROUTE_PARK_EXPIRY_SECONDS},
                }
            }
        ]
    )
    web, route_name = await _open_agent_route(
        stack, token, uniq, agent=_DOOR_AGENT, tag="agkill", reply_expr=_ASK_REPLY_EXPR, resume_expr=_ASK_RESUME_EXPR
    )

    assert (await web.send(uniq("ad-open"))).status_code == 200
    # The park surfaces its question through reply_expr ($asks) — the barrier that the agent run
    # reached the park in the plugin's index.
    await web.frames(until=_out_carrying(question), deadline=60.0)

    thread_id = await _await_route_thread(_api(stack, token), route_name)
    from urllib.parse import urlencode

    await _api(stack, token).delete(f"/api/conversations/{route_name}/thread?{urlencode({'thread_id': thread_id})}")

    # The whole-chain kill tears the agents driver's park index down and delivers the run's single
    # FAILED to the door once; the forgotten thread no longer appears on the route.
    await _assert_door_failed_once(web)
    listing = await _api(stack, token).get(f"/api/conversations/{route_name}/threads")
    assert thread_id not in [item["thread_id"] for item in listing["items"]], listing["items"]


@pytest.mark.skipif(_REAL_LLM, reason="the agent turn runs on the scripted llm_stub; the real leg is on the creds host")
async def test_a_kill_at_a_nested_caller_ask_delivers_one_door_failed_keyed_by_completion(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    question = uniq("nc-q")
    llm_stub.reset()
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": "e2e_caller_ask",
                    "arguments": {"question": question, "expiry_seconds": _ROUTE_PARK_EXPIRY_SECONDS},
                }
            }
        ]
    )
    web, _route_name = await _open_agent_route(
        stack,
        token,
        uniq,
        agent=_DOOR_AGENT,
        tag="nckill",
        reply_expr=_ASK_REPLY_EXPR,
        resume_expr=_ASK_RESUME_EXPR,
        cancel_expr=_ASK_CANCEL_EXPR,
    )

    assert (await web.send(uniq("nc-open"))).status_code == 200
    await web.frames(until=_out_carrying(question), deadline=60.0)

    # A cancel inbound enters AT the nested caller ask (cancel_expr names its id): the kill reads the
    # run's stored delivery address and delivers the run's single FAILED to the door once — keyed by
    # the run's completion, never one per interaction.
    assert (await web.send(_CANCEL_WORD)).status_code == 200
    await _assert_door_failed_once(web)


async def test_a_route_started_user_ask_killed_by_expiry_delivers_one_door_failed(
    agent_route_park_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    api = _api(stack, token)
    identity = uniq("ua-site").replace("_", "-")
    route_name = uniq("ua-route").replace("_", "-")
    execution_key = uniq("ua-exec")
    await _mint_key(api, execution_key)
    # A tool route onto the to="user" park target: the turn parks a user ask out of band under the
    # door's completion binding, then ends silently. No answer arrives — the expiry reaper (pinned
    # to 1s) kills it once its deadline is forced past, and the door's FAILED is what reaches back.
    await api.post(
        f"/api/conversations/{route_name}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "e2e_tool_target_park",
            "execution_key": execution_key,
            "channel": "web",
            "our_identity": identity,
            "start_expr": {"content": "{marker: .message}"},
            "reply_expr": {"content": ".result.answer"},
        },
        expect=200,
    )
    base_url = f"http://{stack.host}:{stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=stack.resources.redis_url)
    assert page.status_code == 200, page.text

    marker = uniq("ua-msg")
    assert (await web.send(marker)).status_code == 200

    async def _parked_iid() -> str | None:
        records = stack.records(f"tool_target_park:{marker}")
        return json.loads(records[0])["interaction_id"] if records else None

    interaction_id = await wait_for_async(_parked_iid, deadline=25.0, message="the route-started user ask never parked")
    await _expire_on(stack, token, interaction_id)

    # The expiry kill delivers the run's single FAILED to the door once.
    await _assert_door_failed_once(web)


@pytest.mark.skipif(_REAL_LLM, reason="the agent turn runs on the scripted llm_stub; the real leg is on the creds host")
async def test_an_agent_route_started_user_ask_killed_by_expiry_delivers_one_door_failed(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, token = agent_route_park_stack
    question = uniq("aua-q")
    # The agent's scripted turn calls the async user-ask probe: the run parks a to="user" ask out of
    # band under the door's completion binding, then ends silently (a SuspendedFinal the door reads).
    # No answer arrives — the expiry reaper (pinned to 1s) kills it once its deadline is forced past,
    # and the door's single FAILED is what reaches back into the transcript.
    llm_stub.reset()
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": "e2e_agent_async_ask",
                    "arguments": {"question": question, "expiry_seconds": _ROUTE_PARK_EXPIRY_SECONDS},
                }
            }
        ]
    )
    _pre = {k for k in stack.record_keys() if k.startswith("agent_async_ask:")}
    web, _route_name = await _open_agent_route(stack, token, uniq, agent="e2e_park_agent", tag="aua")

    assert (await web.send(uniq("aua-open"))).status_code == 200

    async def _parked_iid() -> str | None:
        new = [k[len("agent_async_ask:") :] for k in stack.record_keys() if k.startswith("agent_async_ask:")]
        new = [i for i in new if f"agent_async_ask:{i}" not in _pre]
        return new[0] if len(new) == 1 else None

    interaction_id = await wait_for_async(
        _parked_iid, deadline=60.0, message="the agent route-started user ask never parked"
    )
    await _expire_on(stack, token, interaction_id)

    # The expiry kill delivers the run's single FAILED to the door once.
    await _assert_door_failed_once(web)


async def _expire_on(stack: TaiStack, token: str, interaction_id: str) -> None:
    """Force a live async park's deadline into the past through the ``e2e_expire_park`` MCP probe.

    The stack has access control ON, so the MCP edge is authenticated with the seeded root token."""
    async with stack.mcp(port=stack.port_b, auth=token) as mcp:
        await mcp.call_tool("e2e_expire_park", {"interaction_id": interaction_id}, retry_on_reloading=True)
