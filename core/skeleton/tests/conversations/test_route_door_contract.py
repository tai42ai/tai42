"""The conversation route driving its target through the door contract and the shared visit.

Both target kinds evaluate the route's ``start_expr`` / ``cancel_expr`` / ``resume_expr`` /
``extras_expr`` over the turn payload and drive the target through ``visit``: a null ``start_expr``
starts nothing, an agent's ``start_expr`` builds its ``astream`` kwargs (a route-owned key is
refused), a pending caller ask blocks a fresh agent start, and ``reply_expr`` maps the
run's terminal to the reply.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.agent.events import MessageFinal, StructuredFinal
from tai42_contract.conversations import ConversationRoute
from tai42_contract.interactions import ParkedEntry
from tai42_contract.template import TemplatedText

from tai42_skeleton.app import instance
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import agent_turn as agent_turn_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import overlap as overlap_module
from tai42_skeleton.conversations.turn import record as record_module

from .conftest import (
    FakeChannel,
    FakeManager,
    _settle,
    _store,
    _tool_channel_route,
    _wire,
    _wire_tool,
)

pytestmark = pytest.mark.usefixtures("env")


class _Input(BaseModel):
    user_message: str = ""


class _RecordingAgent(Agent):
    """An agent whose ``astream`` records the kwargs it was driven with and yields fixed events."""

    tool_name = "assistant"
    ToolInput = _Input

    def __init__(self, *events) -> None:
        self._events = events
        self.seen: list[dict] = []

    async def run(self, **kwargs):  # pragma: no cover - astream is overridden
        raise NotImplementedError

    async def astream(self, **kwargs):
        self.seen.append(kwargs)
        for event in self._events:
            yield event


def _agent_route(
    *, start_expr: str | None = None, reply_expr: str | None = None, extras_expr: str | None = None
) -> ConversationRoute:
    return ConversationRoute(
        route_name="chat",
        door="channel",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
        start_expr=TemplatedText(content=start_expr) if start_expr is not None else None,
        reply_expr=TemplatedText(content=reply_expr) if reply_expr is not None else None,
        extras_expr=TemplatedText(content=extras_expr) if extras_expr is not None else None,
    )


def _turn_args(route: ConversationRoute):
    record = record_module._new_record(
        route=route,
        message_id="m-1",
        thread_id="bridge:chat:+15550002222",
        client_address="+15550002222",
        caller_principal=None,
        provider_message_id="PID1",
        inbound_text="hello",
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    return {"record": record, "batch": overlap_module.Batch(lead=record, members=[record])}


def _wire_agent(monkeypatch, route: ConversationRoute, agent: Agent) -> None:
    # ``_wire`` patches the door-contract and reply renderers; the agent turn reads the parked list
    # and the shared visit directly, so only the agent registry needs wiring here.
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"assistant": agent})


async def _run_agent(route: ConversationRoute):
    return await agent_turn_module._run_agent_turn(
        route, "hello", "bridge:chat:+15550002222", "+15550002222", **_turn_args(route)
    )


async def test_tool_route_start_expr_null_starts_nothing(monkeypatch):
    # a ``start_expr`` yielding null starts no dispatch; the turn is a silent outcome.
    route = _tool_channel_route(start_expr="null")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "must not run")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    assert tools.calls == []
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT


async def test_agent_start_expr_owning_thread_id_is_refused(monkeypatch):
    # a ``start_expr`` that sets a route-owned astream key (``thread_id``) is refused loudly.
    agent = _RecordingAgent(MessageFinal(text="unused"))
    route = _agent_route(start_expr='{user_message: {content: .message}, thread_id: "x"}')
    _wire_agent(monkeypatch, route, agent)

    outcome = await _run_agent(route)
    assert isinstance(outcome, outcome_module._ResolvedOutcome)
    assert outcome.answer_status == "error"
    assert "start_expr error" in (outcome.error or "")
    assert agent.seen == []  # never driven


async def test_agent_start_expr_builds_the_astream_kwargs(monkeypatch):
    # The agent is driven with the ``start_expr`` object as its astream kwargs, plus the route's own
    # ``thread_id``; the route text is not the default ``user_message`` when a start_expr is set.
    agent = _RecordingAgent(MessageFinal(text="done"))
    route = _agent_route(start_expr='{user_message: {content: .message}, strategy: "fast"}')
    _wire_agent(monkeypatch, route, agent)

    await _run_agent(route)
    assert len(agent.seen) == 1
    kwargs = agent.seen[0]
    assert kwargs["strategy"] == "fast"
    assert kwargs["user_message"] == {"content": "hello"}
    assert kwargs["thread_id"] == "bridge:chat:+15550002222"


async def test_agent_reply_expr_maps_the_structured_final(monkeypatch):
    # ``reply_expr`` maps the agent's structured final (its ``.data``), not the serialized string.
    agent = _RecordingAgent(StructuredFinal(data={"answer": "the mapped reply"}))
    route = _agent_route(reply_expr=".answer")
    _wire_agent(monkeypatch, route, agent)

    outcome = await _run_agent(route)
    assert isinstance(outcome, outcome_module._ResolvedOutcome)
    assert outcome.answer_status == "answered"
    assert outcome.parts[0].message == "the mapped reply"


async def test_agent_start_is_refused_while_a_caller_ask_is_pending(monkeypatch):
    # a fresh agent start is refused while a caller ask is still ``asking`` on the thread.
    agent = _RecordingAgent(MessageFinal(text="unused"))
    route = _agent_route()
    _wire_agent(monkeypatch, route, agent)

    async def _pending():
        return [ParkedEntry(id="i-1", status="asking", to="caller")]

    monkeypatch.setattr(agent_turn_module, "list_parked", _pending)

    outcome = await _run_agent(route)
    assert isinstance(outcome, outcome_module._ResolvedOutcome)
    assert outcome.answer_status == "error"
    assert "pending" in (outcome.error or "")
    assert agent.seen == []  # the start never ran


class _ExtrasRecordingAgent(_RecordingAgent):
    """A recording agent that declares one door-``extras`` key."""

    extras_keys = frozenset({"warm_start"})


def _wire_agent_declaring_extras(monkeypatch, route: ConversationRoute, agent: Agent) -> None:
    # Seed the process agent binding so BOTH the turn's agent lookup and the visit's declared-extras
    # resolution answer for ``assistant`` from the same registry.
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    monkeypatch.setitem(instance.app._agent_binding._agents, "assistant", agent)


async def test_agent_route_extras_expr_declared_key_runs(monkeypatch):
    # An ``extras_expr`` yielding the agent's declared key passes the visit's extras check and the
    # agent runs.
    agent = _ExtrasRecordingAgent(MessageFinal(text="done"))
    route = _agent_route(extras_expr="{warm_start: .message}")
    _wire_agent_declaring_extras(monkeypatch, route, agent)

    outcome = await _run_agent(route)
    assert isinstance(outcome, outcome_module._ResolvedOutcome)
    assert outcome.answer_status != "error"
    assert len(agent.seen) == 1


async def test_agent_route_extras_expr_undeclared_key_surfaces_the_refusal(monkeypatch):
    # An ``extras_expr`` yielding a key the agent never declared is refused; the refusal surfaces as
    # the turn error and the agent never runs.
    agent = _ExtrasRecordingAgent(MessageFinal(text="unused"))
    route = _agent_route(extras_expr="{nope: .message}")
    _wire_agent_declaring_extras(monkeypatch, route, agent)

    outcome = await _run_agent(route)
    assert isinstance(outcome, outcome_module._ResolvedOutcome)
    assert outcome.answer_status == "error"
    assert "turn error" in (outcome.error or "")
    assert agent.seen == []


async def test_agent_route_parked_bound_as_jq_has_no_null_keys(monkeypatch):
    # A parked entry's unset optional fields are ABSENT from ``$parked``, never null-valued keys: the
    # door binds the one compact shape.
    agent = _RecordingAgent(MessageFinal(text="done"))
    route = _agent_route(
        start_expr=(
            "{user_message: {content: .message}, "
            "parked_nulls: [$parked[0] | to_entries[] | select(.value == null) | .key]}"
        )
    )
    _wire_agent(monkeypatch, route, agent)

    async def _one():
        return [ParkedEntry(id="i-1", status="asking")]

    monkeypatch.setattr(agent_turn_module, "list_parked", _one)

    await _run_agent(route)
    assert agent.seen[0]["parked_nulls"] == []
