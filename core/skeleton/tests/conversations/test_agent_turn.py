"""The agent-target turn: error outcomes, the serialized final, locale, and answer splitting."""

from __future__ import annotations

import json
import time
from typing import cast

import pytest
from tai42_contract.agent import Agent
from tai42_contract.agent.events import MessageFinal, StructuredOutputUnresolvedFinal
from tai42_contract.template import TemplatedText
from tai42_contract.tools import current_call_chain

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import agent_turn as agent_turn_module
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.states.context import current_state_context

from .conftest import (
    EchoAgent,
    FakeChannel,
    FakeManager,
    _channel_route,
    _EchoInput,
    _settle,
    _store,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


async def test_agent_turn_opens_the_push_frame_of_the_target_agent(env, monkeypatch):
    # The agent route opens the outermost minting frame as a PUSH of the target agent's name, so the
    # drive runs under a call chain rooted at the agent and its first ask records ``[agent]``.
    seen: dict[str, tuple[str, ...]] = {}

    class _ChainAgent(EchoAgent):
        async def astream(self, **kwargs):  # type: ignore[override]
            seen["chain"] = current_call_chain()
            yield MessageFinal(text="ok")

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _ChainAgent()})

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert seen["chain"] == ("echo",)


async def test_denied_turn_delivers_an_error_outcome(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    async def _deny(identity, agent_name, **kwargs):
        raise PermissionDeniedError("no run grant")

    monkeypatch.setattr(agent_turn_module, "authorize_execution_agent_run", _deny)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer is not None
    assert "something went wrong" in record.answer.lower()
    assert record.error is not None
    assert "denied" in record.error
    # The error outcome is DELIVERED, not silently dropped.
    assert channel.sends  # a message went out


async def test_agent_structured_array_final_stays_one_serialized_message(env, monkeypatch):
    # An agent whose structured final is a JSON array of strings is
    # serialized to ONE string and delivered as ONE message — NEVER split into ordered parts.
    # An agent's structured output may legitimately be a string array as DATA; only TOOL routes
    # author multi-message parts. Reverting to magic-array detection reddens this.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)

    class _ArrayAgent(Agent):
        tool_name = "arr"
        ToolInput = _EchoInput

        async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
            return ["alpha", "beta", "gamma"]

    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _ArrayAgent()})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    # ONE serialized string, no parts — the array is DATA, not a multi-message answer.
    assert record.answer_parts is None
    assert record.answer == json.dumps(["alpha", "beta", "gamma"])
    assert len(channel.sends) == 1
    assert channel.sends[0].message == json.dumps(["alpha", "beta", "gamma"])


async def test_agent_reprompt_cap_outcome_reaches_the_turn_reply(env, monkeypatch):
    # A capped structured-output run ends on a typed, non-fatal outcome: the conversation route
    # surfaces it to the turn's reply as the serialized typed outcome, never a generic empty-answer
    # error and never a dropped turn.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)

    class _OutcomeAgent(Agent):
        tool_name = "oc"
        ToolInput = _EchoInput

        async def run(self, **kwargs):
            return await self._drain(self.astream(**kwargs))

        async def astream(self, **kwargs):
            yield StructuredOutputUnresolvedFinal(schema_name="Answer", attempts=4, error="value is not an integer")

    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _OutcomeAgent()})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer is not None
    payload = json.loads(record.answer)
    assert payload["type"] == "structured_output_unresolved_final"
    assert payload["attempts"] == 4
    assert len(channel.sends) == 1


async def test_agent_target_ignores_params(env, monkeypatch):
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", {"token": "abc"}
    )
    await _settle()

    # accept() takes params for every target, but the agent turn receives exactly the raw
    # text and nothing else — no params reach the agent.
    assert agent.calls == [("hi", "bridge:line:+15550002222")]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hi"


async def test_a_long_answer_is_split_into_ordered_channel_sends(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    # Past twilio's 1600 cap: several ordered chunks concatenating to the whole answer,
    # never a silent truncation.
    long_text = "x" * 4000
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _fixed_answer_agent(long_text)})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID-L")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert len(channel.sends) > 1
    assert "".join(n.message for n in channel.sends) == record.answer
    assert all(len(n.message) <= 1600 for n in channel.sends)


def _fixed_answer_agent(answer: str) -> Agent:
    class _Fixed(Agent):
        tool_name = "fixed"
        ToolInput = _EchoInput

        async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
            return answer

    return _Fixed()


async def test_missing_channel_in_the_length_map_is_a_loud_failure(env, monkeypatch):

    _wire(monkeypatch, FakeManager(), FakeChannel())
    now = time.time()
    record = ConversationRecord(
        message_id="unmapped",
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="not-in-the-map",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask hello",
        answer_status="answered",
        answer="hello",
        created_at=now,
        updated_at=now,
    )
    await _store().create_record(record)

    with pytest.raises(RuntimeError, match="no max_message_chars entry"):
        await delivery_module.deliver("unmapped")
    # The record is marked failed so the misconfiguration is operationally visible,
    # not left dangling in pending_delivery.
    got = await _store().get_record("unmapped")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.FAILED


async def test_a_routed_channel_that_is_not_registered_is_a_loud_failure(env, monkeypatch):
    # An unloaded channel plugin can never be delivered by retrying, so the record
    # reaches ``failed`` instead of being re-driven by every sweep forever.

    class _NoChannels:
        def get(self, name: str):
            raise KeyError(f"unknown channel {name!r} (registered: [])")

    class _AppWithoutTheChannel:
        channels = _NoChannels()

    _wire(monkeypatch, FakeManager())
    monkeypatch.setattr(delivery_module, "tai42_app", _AppWithoutTheChannel())
    now = time.time()
    await _store().create_record(
        ConversationRecord(
            message_id="unregistered",
            route_name="line",
            door="channel",
            thread_id="bridge:line:+15550002222",
            client_address="+15550002222",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask hello",
            answer_status="answered",
            answer="hello",
            created_at=now,
            updated_at=now,
        )
    )

    with pytest.raises(RuntimeError, match="is not registered on this deployment"):
        await delivery_module.deliver("unregistered")
    got = await _store().get_record("unregistered")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.FAILED
    assert got.attempts == 1


async def test_channel_locale_composes_through_the_turn_into_a_rendered_variant(env, monkeypatch):
    """The composed participant path: a channel accepts a message with the participant's locale, the turn
    carries it onto the subject block AND the ambient state context, and the rendering layer
    resolves the per-locale template variant off that same locale — the flow selects no
    language. Without a variant (and no default) the render refuses loudly."""
    from tai42_contract.storage import Storage

    from tai42_skeleton.storage import StorageRegistry
    from tai42_skeleton.template import ResourceManager
    from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)

    seen: dict[str, object] = {}

    def _fn(kw: dict) -> str:
        ctx = current_state_context()
        seen["subject_block_locale"] = kw["turn"]["subject"]["locale"]
        seen["ambient_locale"] = ctx.candidates.locale if ctx is not None else None
        return "ok"

    _wire_tool(monkeypatch, _fn)

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", locale="he-IL")
    await _settle()

    # Channel -> turn -> subject: the participant's locale reached both the payload subject block
    # and the ambient context the renderer reads.
    assert seen["subject_block_locale"] == "he-IL"
    assert seen["ambient_locale"] == "he-IL"

    # Context locale -> render: the store resolves the he variant off that same locale, and a
    # locale with no variant and no default refuses loudly.
    class _InMemory(Storage):
        def __init__(self) -> None:
            self.items = {"welcome": "default", "welcome@he": "shalom"}

        async def load(self, path: str) -> str:
            try:
                return self.items[path]
            except KeyError as exc:
                raise FileNotFoundError(path) from exc

        async def list(self) -> list[str]:
            return sorted(self.items)

        async def upload(self, path: str, content: str) -> None:
            self.items[path] = content

        async def delete(self, path: str) -> None:
            self.items.pop(path, None)

        async def delete_dir(self, path: str) -> None:
            pass

    registry = StorageRegistry()
    registry.register_storage(_InMemory)
    manager = ResourceManager(registry.provider)

    assert await manager.render_by_id("welcome", locale=cast(str, seen["ambient_locale"])) == "shalom"
    with pytest.raises(TemplateLocaleNotFoundError):
        await manager.render_by_id("missing", locale=cast(str, seen["ambient_locale"]))
