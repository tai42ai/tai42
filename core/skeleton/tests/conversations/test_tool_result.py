"""A tool run's outcome at a conversation turn: a RAISED failure, a returned success, a park, the shape diagnostic.

A failure RAISES ``RunTerminalFailed`` carrying the driver's outcome as an OPAQUE payload — the turn
records the payload WHOLE and delivers the route's client-safe error, never reading a key inside it.
A RETURNED value is a success the route's ``reply_expr`` maps with NO status inspection, so a dict
whose own ``status`` reads failure-adjacent still maps. A still-parked run is a typed
``SuspendedInteraction`` the visit normalises to a silent turn.
"""

from __future__ import annotations

import logging

import pytest
from tai42_contract.interactions import PARK_COMPLETION_SUCCEEDED, RunTerminalFailed, SuspendedInteraction

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import tool_result as tool_result_module
from tai42_skeleton.interactions import terminal_failure as terminal_failure_module

from .conftest import (
    _TURN_LOGGER,
    FakeChannel,
    FakeManager,
    _connected,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


def _raise(outcome):
    """A tool-run callable that RAISES ``RunTerminalFailed`` with ``outcome`` as its opaque payload."""

    def _fn(_kw):
        raise RunTerminalFailed(outcome)

    return _fn


@pytest.mark.parametrize("status", ["aborted", "stopped", "error"])
async def test_a_raised_failure_is_an_error_turn(env, monkeypatch, status):
    # A failed terminal RAISES RunTerminalFailed; the turn records its payload and delivers the
    # route's client-safe error — the half-finished run is never answered as a completed one.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, _raise({"status": status, "result": {"reply": "half-finished"}}))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    # The failure stays diagnosable (the whole payload is recorded)...
    assert record.error is not None
    assert status in record.error
    # ...and the partial payload never reached the participant.
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_a_raised_failures_payload_is_recorded_whole_and_opaque(env, monkeypatch):
    # The turn records the payload WHOLE — every field the driver wrote, verbatim — never pulling
    # ``error_kind`` / ``missing_results`` / ``session_id`` out by name. The whole dict's repr is
    # the detail, so a token in any field survives, and the partial ``result`` is recorded too (it
    # is part of the opaque payload the driver owns), never delivered.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(
        monkeypatch,
        _raise(
            {
                "status": "error",
                "result": {"reply": "half-finished"},
                "error": "node raised at step two",
                "error_kind": "node_failure",
                "missing_results": ["gamma-node-id"],
                "session_id": "run-quebec",
            }
        ),
    )

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.error is not None
    # The whole payload is recorded opaquely — its fields appear in the detail without being keyed.
    for fragment in ("node raised at step two", "node_failure", "gamma-node-id", "run-quebec"):
        assert fragment in record.error
    # The partial payload is never delivered to the participant.
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT


async def test_a_raised_failures_payload_is_capped_with_a_truncation_marker(env, monkeypatch):
    # A payload large enough to bloat the record is clipped — and says so, so a truncated detail
    # never reads as a complete one.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, _raise({"status": "error", "error": "boom-" + "x" * 10_000}))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.error is not None
    assert terminal_failure_module.FAILED_OUTCOME_DETAIL_ELLIPSIS in record.error
    # Bounded by the cap plus the surrounding detail wording, nowhere near the 10KB value.
    assert len(record.error) < 2 * terminal_failure_module.FAILED_OUTCOME_DETAIL_LIMIT
    assert "boom-" in record.error


async def test_a_raised_failure_uses_the_route_error_reply_text(env, monkeypatch):
    # A raised failure is surfaced with the route's own participant-facing wording when it carries one.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null", error_reply_text=spanish)
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, _raise({"status": "aborted", "result": {"reply": "half-finished"}}))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [spanish]


@pytest.mark.parametrize("status", ["aborted", "stopped", "error", "suspended", "interrupt", "queued"])
async def test_a_returned_dict_with_a_status_key_is_delivered_as_a_success(env, monkeypatch, status):
    # A RETURNED value is a SUCCESS the route maps with NO status inspection — the turn is the
    # neutral consumer: a success payload that happens to carry a ``status`` key (any value) is a
    # plain field that keeps mapping, never mistaken for a failure or a pause.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"status": status, "result": {"reply": "the real answer"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == "the real answer"
    assert [n.message for n in channel.sends] == ["the real answer"]


def _park() -> SuspendedInteraction:
    """A still-parked typed value a tool returns for a user-only async park (no caller asks)."""
    return SuspendedInteraction(interaction_id="i-async", interaction_ids=["i-async"], caller_interaction_ids=[])


async def test_a_parked_typed_value_ends_the_turn_silently(env, monkeypatch):
    # A still-parked run returns a SuspendedInteraction typed value; the visit normalises a
    # user-only park to a silent turn — no reply, no error reply — and the real reply delivers out
    # of band when the resume drives past the pause.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: _park())

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer is None
    assert record.answer_status is None
    assert channel.sends == []


async def test_a_mapping_failure_logs_the_value_free_result_shape(env, monkeypatch, caplog):
    # A success terminal whose flagged nodes ride DIRECTLY under `result` (no `result.outputs`
    # level) mapped by a reply_expr reading a stale `.result.outputs.*` path: the guard raises and
    # the turn takes the mapping-failure arm. The diagnostic names the shape mismatch while NEVER
    # logging a participant-content value.
    guard = (
        ".result.outputs.compose_messages as $c "
        "| if (($c // []) | length) == 0 "
        'then error("the turn finished without a reply payload (outputs.compose_messages is empty)") '
        "else $c end"
    )
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=guard)
    _wire(monkeypatch, FakeManager(route), channel)
    secret = "PARTICIPANTSECRET-zulu-must-not-be-logged"
    envelope = {
        "status": "success",
        "result": {"compose_messages": {"messages": [{"text": secret}]}, "extract_todo": {"items": []}},
        "missing_results": ["build_state_language", "build_state_consent"],
    }
    _wire_tool(monkeypatch, lambda kw: envelope)

    with caplog.at_level(logging.ERROR, logger=_TURN_LOGGER):
        message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
        await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "reply_expr" in record.error
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT

    shape_logs = [
        r.getMessage()
        for r in caplog.records
        if r.name == _TURN_LOGGER and "had shape:" in r.getMessage() and "tool-line" in r.getMessage()
    ]
    assert len(shape_logs) == 1
    shape = shape_logs[0]
    assert "status='success'" in shape
    assert "result_keys=['compose_messages', 'extract_todo']" in shape
    assert "outputs_surface_sizes" not in shape
    assert "missing_results=['build_state_consent', 'build_state_language']" in shape
    assert secret not in shape
    assert all(secret not in r.getMessage() for r in caplog.records)


async def test_result_shape_is_value_free_and_structural():
    # Unit-pins the descriptor directly: it renders NAMES, the status token, surface SIZES and the
    # missing_results names — never a participant VALUE, and never crashes on odd inputs.
    secret = "PLAINTEXT-should-never-appear"
    shape = tool_result_module._result_shape(
        {
            "status": "success",
            "result": {"outputs": {"compose_messages": [{"text": secret}], "note": secret}},
            "missing_results": ["compose_messages", "extract_todo"],
        }
    )
    assert "status='success'" in shape
    assert "keys=['missing_results', 'result', 'status']" in shape
    assert "'compose_messages'" in shape
    assert "'note'" in shape
    assert "missing_results=['compose_messages', 'extract_todo']" in shape
    assert secret not in shape
    # A non-dict result degrades to a type + length, never a repr of the value.
    assert tool_result_module._result_shape("the reply text") == "type=str len=14"
    assert tool_result_module._result_shape(42) == "type=int"


async def test_a_parked_typed_value_binds_completion_and_delivers_on_resume(env, monkeypatch):
    # End-to-end: a parked run returns a SuspendedInteraction, the turn ends silently, and the
    # generic tool-route completion is bound around the dispatch naming THIS thread — so when the
    # resume drives past the pause and fires deliver_tool_completion, the route's reply_expr maps the
    # final result and it is delivered back into the thread.
    from tai42_contract.interactions import get_park_completion

    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.outputs.compose_messages")
    _wire(monkeypatch, FakeManager(route), channel)

    bound: list = []

    def _pause(kw):
        bound.append(get_park_completion())
        return _park()

    _wire_tool(monkeypatch, _pause)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []
    assert len(bound) == 1
    tool_name, context = bound[0]
    assert tool_name == turn_module.DELIVER_TOOL_COMPLETION_NAME
    thread_id = context["delivery_thread_id"]
    assert thread_id == "bridge:tool-line:+15550002222"
    assert context["route_name"] == "tool-line"

    from tai42_skeleton.runs.chokepoint import delivery_fire

    with delivery_fire("c-resume"):
        out = await turn_module.deliver_tool_completion(
            delivery_thread_id=thread_id,
            completion_id="c-resume",
            result={"result": {"outputs": {"compose_messages": "your quote is ready"}}},
            status=PARK_COMPLETION_SUCCEEDED,
        )
    await _settle()
    assert out == {"message_id": "c-resume"}
    delivered = await _store().get_record("c-resume")
    assert delivered is not None
    assert delivered.answer == "your quote is ready"
    assert delivered.delivery_status is DeliveryStatus.DELIVERED
    assert "your quote is ready" in [n.message for n in channel.sends]


async def test_a_parked_typed_value_over_the_api_door_delivers_a_silent_marker(env, monkeypatch):
    # Parity over the api door: an api-door tool turn that parks returns the explicit silent marker
    # through the durable machine (the door promised a callback), never an error notice.
    route = _tool_api_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route))
    _wire_tool(monkeypatch, lambda kw: _park())

    async def _post(url, body, signature, timeout_seconds):
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "tool-api", "user-7", "hi", "alice", wait_seconds=5, client_connected=_connected
    )
    await _settle()

    assert result.answer is not None
    assert result.answer.status == "silent"
    assert result.answer.answer is None
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
