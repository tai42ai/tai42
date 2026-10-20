"""The participant-facing reply a target's terminal failure carries, on every conversation door.

A target that fails with ``RunTerminalFailed(outcome, reply=...)`` (or returns ``RunFailed(reply=...)``
from a resume) has its ``reply`` shown to the participant in place of the door's fixed failure text;
with no reply the door falls back to the route's ``error_reply_text``, else the built-in line. The
internal detail of the opaque ``outcome`` stays in the record's ``error`` field. Every platform-owned
failure keeps the fixed text.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    PARK_COMPLETION_WITHDRAWN,
    RunTerminalFailed,
)
from tai42_contract.template import TemplatedText
from tests.agent._memoryless import MemorylessTestAgent

from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module
from tai42_skeleton.interactions.terminal_failure import failed_outcome_detail
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.runs.chokepoint import delivery_fire

from .conftest import (
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

_REPLY = "Sorry, that option is no longer available. Pick another?"
_ROUTE_TEXT = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
_OUTCOME = {"status": "error", "error": "upstream 503: internal stack detail"}

_INVALID_REPLIES = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("x" * 2001, id="too-long"),
    pytest.param(5, id="non-string"),
]


async def _accept_turn() -> str:
    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    return message_id


def _raising(failure: RunTerminalFailed):
    def _fn(_kw):
        raise failure

    return _fn


# --- the one chokepoint ------------------------------------------------------------------


def test_the_failure_text_is_the_reply_else_the_route_text_else_the_built_in_line():
    assert outcome_module._failure_answer_text(_tool_channel_route(error_reply_text=_ROUTE_TEXT), _REPLY) == _REPLY
    assert outcome_module._failure_answer_text(_tool_channel_route(error_reply_text=_ROUTE_TEXT), None) == _ROUTE_TEXT
    assert outcome_module._failure_answer_text(_tool_channel_route(), None) == outcome_module._ERROR_ANSWER_TEXT
    assert outcome_module._failure_answer_text(None, None) == outcome_module._ERROR_ANSWER_TEXT
    assert outcome_module._failure_answer_text(None, _REPLY) == _REPLY


def test_a_terminal_failure_resolves_to_its_reply_and_records_the_detail():
    resolved = outcome_module._terminal_failure(RunTerminalFailed(_OUTCOME, reply=_REPLY), _tool_channel_route())
    assert resolved.answer_status == "error"
    assert resolved.answer == _REPLY
    assert resolved.error == failed_outcome_detail(_OUTCOME)


# --- a live turn (tool route, agent route) -----------------------------------------------


async def test_a_tool_turn_failing_with_a_reply_answers_with_the_reply(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(error_reply_text=_ROUTE_TEXT)), channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME, reply=_REPLY)))

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    # The target's reply wins over the route's error_reply_text...
    assert record.answer == _REPLY
    assert [n.message for n in channel.sends] == [_REPLY]
    # ...and the internal detail stays in the record's error field, never in the answer.
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert "upstream 503" not in record.answer


async def test_a_tool_turn_failing_without_a_reply_answers_with_the_route_text(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(error_reply_text=_ROUTE_TEXT)), channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _ROUTE_TEXT
    assert record.error == failed_outcome_detail(_OUTCOME)


class _FailingAgent(MemorylessTestAgent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self, failure: RunTerminalFailed) -> None:
        self.failure = failure

    async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        raise self.failure


@pytest.mark.parametrize(
    ("reply", "expected"),
    [pytest.param(_REPLY, _REPLY, id="reply"), pytest.param(None, _ROUTE_TEXT, id="no-reply")],
)
async def test_an_agent_turn_failing_answers_with_its_reply_else_the_route_text(env, monkeypatch, reply, expected):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(error_reply_text=_ROUTE_TEXT)), channel)
    agent = _FailingAgent(RunTerminalFailed(_OUTCOME, reply=reply))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == expected
    assert [n.message for n in channel.sends] == [expected]
    assert record.error == failed_outcome_detail(_OUTCOME)


# --- the completion addresses ------------------------------------------------------------


async def _fire(tool, **kw):
    with delivery_fire(kw["completion_id"]):
        return await tool(**kw)


_COMPLETION_TOOLS = [
    pytest.param(turn_module.deliver_tool_completion, _tool_channel_route, "bridge:tool-line:+15550002222", id="tool"),
    pytest.param(turn_module.deliver_agent_completion, _channel_route, "bridge:line:+15550002222", id="agent"),
]


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
async def test_a_failed_completion_carrying_a_reply_delivers_the_reply(env, monkeypatch, tool, make_route, thread_id):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route(error_reply_text=_ROUTE_TEXT)), channel)

    out = await _fire(
        tool, thread_id=thread_id, completion_id="c-1", result=_OUTCOME, status=PARK_COMPLETION_FAILED, reply=_REPLY
    )
    await _settle()

    assert out == {"message_id": "c-1"}
    record = await _store().get_record("c-1")
    assert record is not None
    assert record.answer == _REPLY
    assert [n.message for n in channel.sends] == [_REPLY]


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
async def test_a_failed_completion_without_a_reply_keeps_the_route_text(env, monkeypatch, tool, make_route, thread_id):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route(error_reply_text=_ROUTE_TEXT)), channel)

    await _fire(tool, thread_id=thread_id, completion_id="c-1", result=_OUTCOME, status=PARK_COMPLETION_FAILED)
    await _settle()

    record = await _store().get_record("c-1")
    assert record is not None
    assert record.answer == _ROUTE_TEXT


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
@pytest.mark.parametrize("reply", _INVALID_REPLIES)
async def test_a_completion_refuses_an_invalid_reply_and_writes_no_record(
    env, monkeypatch, tool, make_route, thread_id, reply
):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route()), channel)

    with pytest.raises(ValidationError):
        await _fire(
            tool, thread_id=thread_id, completion_id="c-1", result=_OUTCOME, status=PARK_COMPLETION_FAILED, reply=reply
        )
    await _settle()

    assert await _store().get_record("c-1") is None
    assert channel.sends == []


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
@pytest.mark.parametrize("status", [PARK_COMPLETION_SUCCEEDED, PARK_COMPLETION_WITHDRAWN, None, "stopped"])
async def test_a_completion_refuses_a_reply_on_a_non_failed_status(
    env, monkeypatch, tool, make_route, thread_id, status
):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route()), channel)

    with pytest.raises(ValueError, match="only a failed terminal carries a reply"):
        await _fire(tool, thread_id=thread_id, completion_id="c-1", result="hello", status=status, reply=_REPLY)
    await _settle()

    assert await _store().get_record("c-1") is None
    assert channel.sends == []


# --- the platform-owned failures keep the fixed text --------------------------------------


@asynccontextmanager
async def _deny_bind(execution_key, *, bound_fingerprint):
    raise PermissionDeniedError("the execution key carries no authority")
    yield  # pragma: no cover - makes this an async generator


async def _door_contract_error(*_args, **_kwargs):
    raise RuntimeError("door contract blew up")


def _tool_raises(_kw):
    raise RuntimeError("tool blew up")


@pytest.mark.parametrize(
    ("reply_expr", "tool_fn", "patch"),
    [
        pytest.param(None, _tool_raises, None, id="generic-exception"),
        pytest.param(None, lambda _kw: {"unexpected": "dict"}, None, id="wrong-typed-result"),
        pytest.param(".count", lambda _kw: {"count": 42}, None, id="reply-expr-error"),
        pytest.param(None, lambda _kw: "unreachable", ("bind_execution_identity", _deny_bind), id="permission-denied"),
        pytest.param(
            None, lambda _kw: "unreachable", ("evaluate_door_contract", _door_contract_error), id="door-contract-error"
        ),
    ],
)
async def test_a_platform_owned_tool_turn_failure_keeps_the_route_text(env, monkeypatch, reply_expr, tool_fn, patch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_expr=reply_expr, error_reply_text=_ROUTE_TEXT)), channel)
    _wire_tool(monkeypatch, tool_fn)
    if patch is not None:
        monkeypatch.setattr(tool_turn_module, *patch)

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _ROUTE_TEXT
    assert [n.message for n in channel.sends] == [_ROUTE_TEXT]


class _EmptyAgent(MemorylessTestAgent):
    tool_name = "echo"
    ToolInput = _EchoInput

    async def run(self, **kwargs):
        return ""


@pytest.mark.parametrize(
    "registry",
    [pytest.param(lambda: {"echo": _EmptyAgent()}, id="empty-answer"), pytest.param(dict, id="unregistered-agent")],
)
async def test_a_platform_owned_agent_turn_failure_keeps_the_built_in_line(env, monkeypatch, registry):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", registry)

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
