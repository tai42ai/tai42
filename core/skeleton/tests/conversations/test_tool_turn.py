"""The tool-target turn: payload/reply mapping, silent and error outcomes, and api-door silent delivery."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from tai42_contract.conversations import (
    TargetConversationConfig,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import agent_turn as agent_turn_module
from tai42_skeleton.conversations.turn import keys as keys_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import record as record_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module
from tai42_skeleton.operations.errors import PermissionDenied

from .conftest import (
    FakeChannel,
    FakeManager,
    _accepting_callback,
    _FakeTemplateApp,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


async def test_tool_target_delivers_its_string_reply(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    tools = _wire_tool(monkeypatch, lambda kw: f"tool saw {kw['message']} from {kw['sender']}")

    message_id = await turn_module.accept("twilio", "+15550001111", " +15550002222 ", " +15550002222 ", "hi", "PID1")
    await _settle()

    # The default kwargs are {message, sender, turn} — canonical address for the sender, the
    # generic turn block naming this channel message — and the sync tool is offloaded off
    # the event loop.
    assert tools.calls == [
        {
            "key": "echo-tool",
            "arguments": {
                "message": "hi",
                "sender": "+15550002222",
                "turn": {
                    "id": message_id,
                    "inbound": {"id": "PID1", "kind": "message", "source": "twilio"},
                    "subject": {
                        "target_kind": "tool",
                        "target_name": "echo-tool",
                        "person": None,
                        "thread": "bridge:tool-line:+15550002222",
                        "locale": None,
                    },
                },
            },
            "offload_sync": True,
        }
    ]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == "tool saw hi from +15550002222"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == ["tool saw hi from +15550002222"]


@pytest.mark.parametrize("reply", [None, "", "   "])
async def test_tool_target_none_or_blank_reply_is_silent(env, monkeypatch, reply):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: reply)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    # Terminal SILENT: no answer, no answer_status, nothing sent — by design, not an error.
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer is None
    assert record.answer_status is None
    assert channel.sends == []


async def test_tool_target_payload_expr_maps_the_kwargs(env, monkeypatch):
    # A preset-shaped payload: an expr emitting {"example_config_kwargs": {...}}.
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr="{example_config_kwargs: {text: .message, from: .sender}}")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "run it", "PID1")
    await _settle()

    assert tools.calls[0]["arguments"] == {"example_config_kwargs": {"text": "run it", "from": "+15550002222"}}


async def test_tool_target_kwargs_carry_the_turn_thread_id(env, monkeypatch):
    # Composed accept→turn→dispatch: the routed flow/tool's received kwargs carry the thread_id
    # matching the thread this turn ran under — the same opaque id the thread doors address.
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr="{tid: .thread_id}")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "run it", "PID1")
    await _settle()

    expected_thread = keys_module._thread_id("tool-line", "+15550002222")
    assert tools.calls[0]["arguments"] == {"tid": expected_thread}
    # The kwargs' thread_id is the very thread the turn's record was written under — the id
    # the thread doors accept (``DELETE .../thread?thread_id=``).
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.thread_id == expected_thread


async def test_tool_target_reply_expr_over_an_envelope_dict(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"result": {"reply": "from the envelope"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "from the envelope"
    assert [n.message for n in channel.sends] == ["from the envelope"]


async def test_tool_target_reply_expr_yielding_null_is_silent(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"result": {}})  # .reply // null -> null

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []


async def test_tool_target_success_status_maps_through_reply_expr(env, monkeypatch):
    # The success terminal is the mapped one — unchanged.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"status": "success", "result": {"reply": "all done"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert [n.message for n in channel.sends] == ["all done"]


@pytest.mark.parametrize(
    "result",
    [
        # No status key at all — the arbitrary dict most tools return.
        {"result": {"reply": "all done"}},
        # A status key from a tool's OWN vocabulary: not a terminal-outcome name, so it stays
        # on the reply path — the turn diverts only on a named non-success terminal.
        {"status": "queued", "result": {"reply": "all done"}},
        {"status": "not_found", "result": {"reply": "all done"}},
        # A status REPORT about a run is a successful answer, not a terminal of THIS call.
        {"status": "errored", "result": {"reply": "all done"}},
        # The park-completion FIRE vocabulary is not a returned-envelope terminal either.
        {"status": "failed", "result": {"reply": "all done"}},
        # Matching is case-sensitive, so a look-alike casing is a tool's own vocabulary.
        {"status": "Aborted", "result": {"reply": "all done"}},
        # A null/non-string status is a plain payload field, never a terminal name.
        {"status": None, "result": {"reply": "all done"}},
        {"status": 500, "result": {"reply": "all done"}},
        {"status": ["error"], "result": {"reply": "all done"}},
    ],
)
async def test_tool_target_result_without_a_terminal_status_still_maps_through_reply_expr(env, monkeypatch, result):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: result)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert [n.message for n in channel.sends] == ["all done"]


async def test_tool_target_suspended_interaction_ends_the_turn_silently(env, monkeypatch):
    # A tool that async-parks the caller returns the generic SuspendedInteraction
    # sentinel; the turn recognizes it by TYPE (never a reply_expr mapping) and ends
    # silently — the parked continuation resumes work out of band.
    from tai42_contract.interactions import SuspendedInteraction

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: SuspendedInteraction(interaction_id="i-async"))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer is None
    assert record.answer_status is None
    assert channel.sends == []


async def test_tool_target_wrong_typed_result_is_an_error(env, monkeypatch):
    # No reply_expr, so a non-null/non-string result is a turn error, not a silent drop.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: {"unexpected": "dict"})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_that_raises_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)

    def _boom(kw):
        raise RuntimeError("tool blew up")

    _wire_tool(monkeypatch, _boom)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "tool blew up" in record.error
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_a_channel_tool_error_uses_the_route_error_reply_text_when_set(env, monkeypatch):
    # A route carrying ``error_reply_text`` sends THAT participant-facing reply on a failed turn,
    # while the record's internal ``error`` detail keeps the diagnosable wording unchanged.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(error_reply_text=spanish)), channel)

    def _boom(kw):
        raise RuntimeError("tool blew up")

    _wire_tool(monkeypatch, _boom)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    # The participant sees the route's custom reply, not the built-in default.
    assert record.answer == spanish
    assert [n.message for n in channel.sends] == [spanish]
    # The internal detail is untouched — only the participant-facing answer resolves through the route.
    assert record.error is not None
    assert "tool blew up" in record.error


async def test_a_channel_tool_error_falls_back_to_the_default_when_unset(env, monkeypatch):
    # With no ``error_reply_text`` the same failure delivers the built-in default reply.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)

    def _boom(kw):
        raise RuntimeError("tool blew up")

    _wire_tool(monkeypatch, _boom)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_payload_expr_multi_emit_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".message, .sender")  # two emits
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "unreachable")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls == []  # the tool never ran
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_payload_expr_non_object_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".message")  # emits a string, not an object
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "unreachable")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls == []  # the mapping failed before the tool ran
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_reply_expr_multi_emit_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".a, .b")  # two emits
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"a": "one", "b": "two"})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_reply_expr_non_string_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".count")  # emits a number
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"count": 42})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_denied_dispatch_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: "unreachable")

    @asynccontextmanager
    async def _deny_bind(execution_key, *, bound_fingerprint):
        raise PermissionDenied("the execution key carries no authority")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(agent_turn_module, "bind_execution_identity", _deny_bind)
    monkeypatch.setattr(tool_turn_module, "bind_execution_identity", _deny_bind)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "denied" in record.error
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_dispatch_is_offloaded_off_the_event_loop(env, monkeypatch):
    # A synchronous tool must run off the loop, or a blocking dispatch starves the turn
    # engine; the bridge always dispatches with offload_sync set.
    _wire(monkeypatch, FakeManager(_tool_channel_route()), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls[0]["offload_sync"] is True


async def test_tool_target_payload_expr_reads_our_identity_and_channel(env, monkeypatch):
    # our_identity and channel reach the tool through payload_expr; on the channel door they
    # carry the route's real values.
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr="{oid: .our_identity, ch: .channel}")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls[0]["arguments"] == {"oid": "+15550001111", "ch": "twilio"}


async def test_tool_target_payload_expr_our_identity_and_channel_are_null_on_the_api_door(env, monkeypatch):
    # An api route carries no channel/our_identity, so both read as null through payload_expr.
    route = _tool_api_route(payload_expr="{oid: .our_identity, ch: .channel}")
    _wire(monkeypatch, FakeManager(route))
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=5)
    await _settle()

    assert tools.calls[0]["arguments"] == {"oid": None, "ch": None}


async def test_tool_target_over_the_api_door_silent_delivers_a_signed_silent_callback(env, monkeypatch):
    # A silent api-door tool turn keeps its 202 promise through a signed callback carrying
    # the explicit silent marker and NO answer field.
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    _wire_tool(monkeypatch, lambda kw: None)
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature, json.loads(body)))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=0)
    assert result.answer is None  # 202: the outcome is delivered out of band
    await _settle()

    assert len(posted) == 1  # exactly one callback, no double-fire
    url, signature, body = posted[0]
    assert url == "https://cb.example/x"
    assert signature.startswith("sha256=")
    assert body == {"message_id": result.message_id, "thread_id": result.thread_id, "status": "silent"}
    assert "answer" not in body
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.answer_status == "silent"
    assert record.answer is None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_tool_target_over_the_api_door_silent_sync_wait_returns_the_marker(env, monkeypatch):
    # A silent turn finishing inside the sync wait returns the silent marker inline (200)
    # with its callback suppressed, exactly as an answered turn is.
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    _wire_tool(monkeypatch, lambda kw: None)
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=5)
    await _settle()

    assert result.answer is not None
    assert result.answer.status == "silent"
    assert result.answer.answer is None
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert posted == []  # the sync wait delivered it; no callback fired


async def test_tool_turn_deposits_the_route_state_binding_on_the_ambient_invocation(env, monkeypatch) -> None:
    # The real ``_run_tool_turn`` seam reads the target config's binding and deposits it on the
    # ambient ToolInvocation around the tool dispatch, so the chokepoint carries it forward.
    from tai42_contract.states import StateAttach, StateBinding
    from tai42_contract.tools import current_tool_invocation

    binding = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".thread_id"))])
    route = _tool_channel_route(payload_expr=".")

    class _Cfg:
        async def get(self, target_kind, target_name):
            return TargetConversationConfig(target_kind=target_kind, target_name=target_name, state_binding=binding)

    monkeypatch.setattr(accessors_module, "_config_store", lambda: _Cfg())
    monkeypatch.setattr(tool_turn_module, "tai42_app", _FakeTemplateApp())

    seen: dict = {}

    class _RecordingTools:
        async def run_tool(self, key, arguments, *, offload_sync=False):
            inv = current_tool_invocation()
            seen["binding"] = inv.state_binding if inv is not None else None
            return "ok"

    monkeypatch.setattr(accessors_module, "_tools", lambda: _RecordingTools())

    record = record_module._new_record(
        route=route,
        message_id="m-x",
        thread_id="bridge:tool-line:+15550002222",
        client_address="+15550002222",
        caller_principal=None,
        provider_message_id="PID1",
        inbound_text="hello",
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    await tool_turn_module._run_tool_turn(
        route, "hello", "bridge:tool-line:+15550002222", "+15550002222", record=record
    )
    assert seen["binding"] == binding
