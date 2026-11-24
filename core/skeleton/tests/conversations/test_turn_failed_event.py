"""The ``conversations_turn_failed`` platform event: emitted once for every stored failed turn.

A failed turn emits the event once its record's write has landed, whoever sent the turn and whether
or not the participant is sent a line: a live message turn, a live event turn, a stranded turn the
re-drive fails and a parked turn's failed completion alike. A successful turn emits nothing, and a
re-drive that lost its guard (the outcome it built was never stored) emits nothing. A hooks-manager
fault never changes the stored outcome.
"""

from __future__ import annotations

from types import SimpleNamespace

from tai42_contract.conversations import ReplyTexts
from tai42_contract.interactions import PARK_COMPLETION_FAILED, RunTerminalFailed

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn.failed_turn import TURN_FAILED_EVENT_TOPIC
from tai42_skeleton.hooks import cache as hooks_cache
from tai42_skeleton.interactions.terminal_failure import failed_outcome_detail

from .conftest import (
    EchoAgent,
    FakeChannel,
    FakeManager,
    _channel_route,
    _connected,
    _create_stranded_intake,
    _settle,
    _store,
    _tool_channel_route,
    _wire,
    _wire_tool,
)
from .test_redrive import _accepted_event_record, _strand_event_record
from .test_target_failure_reply import (
    _OUTCOME,
    _ROUTE_TEXT,
    _accept_turn,
    _event_submission,
    _fire,
    _open_channel_thread,
    _raising,
)


class _RecordingHooksManager:
    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.events.append(SimpleNamespace(topic=topic, payload=payload))

    def turn_failed(self) -> list[dict]:
        return [event.payload for event in self.events if event.topic == TURN_FAILED_EVENT_TOPIC]


class _FailingHooksManager:
    def __init__(self) -> None:
        self.calls = 0

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.calls += 1
        raise RuntimeError("hooks backend unreachable")


def _recording_hooks(monkeypatch) -> _RecordingHooksManager:
    hooks = _RecordingHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    return hooks


def test_the_topic_name_is_the_documented_one():
    assert TURN_FAILED_EVENT_TOPIC == "conversations_turn_failed"


async def test_a_failed_message_turn_emits_the_event_once(env, monkeypatch):
    hooks = _recording_hooks(monkeypatch)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == _ROUTE_TEXT
    assert hooks.turn_failed() == [
        {
            "message_id": message_id,
            "thread_id": "bridge:tool-line:+15550002222",
            "route_name": "tool-line",
            "door": "channel",
            "channel": "twilio",
            "client_address": "+15550002222",
            "inbound_kind": "message",
            "error": failed_outcome_detail(_OUTCOME),
        }
    ]


async def test_a_failed_event_turn_emits_the_event_once(env, monkeypatch):
    hooks = _recording_hooks(monkeypatch)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    await _open_channel_thread(env, monkeypatch, channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    result = await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222"), "svc-int", client_connected=_connected
    )
    await _settle()

    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []
    assert hooks.turn_failed() == [
        {
            "message_id": result.message_id,
            "thread_id": "bridge:tool-line:+15550002222",
            "route_name": "tool-line",
            "door": "channel",
            "channel": "twilio",
            "client_address": "+15550002222",
            "inbound_kind": "event",
            "error": failed_outcome_detail(_OUTCOME),
        }
    ]


async def test_a_successful_turn_emits_no_event(env, monkeypatch):
    hooks = _recording_hooks(monkeypatch)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda _kw: "all good")

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert hooks.turn_failed() == []


async def test_a_stranded_turn_the_redrive_fails_emits_the_event_once(env, monkeypatch):
    hooks = _recording_hooks(monkeypatch)
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    await turn_module.redrive_accepted()
    await _settle()

    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    assert hooks.turn_failed() == [
        {
            "message_id": "stranded",
            "thread_id": "bridge:line:+15550002222",
            "route_name": "line",
            "door": "channel",
            "channel": "twilio",
            "client_address": "+15550002222",
            "inbound_kind": "message",
            "error": "turn was interrupted before it produced an answer",
        }
    ]


async def test_a_redrive_that_loses_the_guard_emits_no_event(env, monkeypatch):
    hooks = _recording_hooks(monkeypatch)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    store = _store()
    record = _accepted_event_record("raced-event")
    await _strand_event_record(store, env, record)
    assert await store.claim_event("tool-line", "evt-1", "raced-event") == "raced-event"

    async def _lost_the_guard(self, completed):
        return 0

    monkeypatch.setattr(records_module.ConversationRecordStore, "complete_record", _lost_the_guard)

    await turn_module.redrive_accepted()
    await _settle()

    assert hooks.turn_failed() == []
    assert channel.sends == []


async def test_a_failed_completion_of_a_parked_turn_emits_the_event_once(env, monkeypatch):
    hooks = _recording_hooks(monkeypatch)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)

    out = await _fire(
        turn_module.deliver_tool_completion,
        thread_id="bridge:tool-line:+15550002222",
        completion_id="c-1",
        result=_OUTCOME,
        status=PARK_COMPLETION_FAILED,
        inbound_kind="event",
    )
    await _settle()

    assert out == {"message_id": "c-1"}
    assert channel.sends == []
    assert hooks.turn_failed() == [
        {
            "message_id": "c-1",
            "thread_id": "bridge:tool-line:+15550002222",
            "route_name": "tool-line",
            "door": "channel",
            "channel": "twilio",
            "client_address": "+15550002222",
            "inbound_kind": "event",
            "error": failed_outcome_detail(_OUTCOME),
        }
    ]


async def test_a_hooks_manager_fault_leaves_the_failed_turn_stored_as_it_was(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))
    hooks = _FailingHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert [n.message for n in channel.sends] == [_ROUTE_TEXT]
    assert hooks.calls == 1
