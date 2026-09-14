"""Classifying a tool run's returned envelope: failures, suspends, interrupts, and the shape diagnostic."""

from __future__ import annotations

import logging

import pytest
from tai42_contract.interactions import PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import tool_result as tool_result_module

from .conftest import (
    _TURN_LOGGER,
    FakeChannel,
    FakeManager,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


@pytest.mark.parametrize("status", ["aborted", "stopped", "error"])
async def test_tool_target_result_naming_a_non_success_terminal_is_an_error(env, monkeypatch, status):
    # A result envelope that NAMES a non-success terminal carries a partial (or empty) result
    # its reply_expr would happily map into a reply that reads like a completed run. The turn
    # reads the status first: the mapping never runs and the turn is a failed one.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"status": status, "result": {"reply": "half-finished"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    # The status is VISIBLE in the recorded detail, so the failure is diagnosable...
    assert record.error is not None
    assert status in record.error
    # ...and the partial payload never reached the participant.
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_non_success_detail_carries_every_failure_key(env, monkeypatch):
    # The recorded detail names WHY the terminal failed — the envelope's own failure keys — so
    # an operator reading the record sees the failure, the outputs the run never produced and
    # the run handle to trace it by, without the raw partial payload ever being delivered.
    # Each key carries a token appearing NOWHERE else in the fixture, so dropping any one key
    # from the rendering fails this test.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(
        monkeypatch,
        lambda kw: {
            "status": "error",
            "result": {"reply": "half-finished"},
            "error": "node raised at step two",
            "error_kind": "node_failure",
            "last_error": "TimeoutError-zulu",
            "missing_results": ["gamma-node-id"],
            "session_id": "run-quebec",
        },
    )

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.error is not None
    # The rendered KEY and its value, together: a bare token would also match a neighbouring
    # key's value, and a bare ``error=`` is a substring of ``last_error=``.
    for fragment in (
        "error='node raised at step two'",
        "error_kind='node_failure'",
        "last_error='TimeoutError-zulu'",
        "missing_results=['gamma-node-id']",
        "session_id='run-quebec'",
    ):
        assert fragment in record.error
    # The partial payload stays out of the detail entirely.
    assert "half-finished" not in record.error


async def test_tool_target_non_success_detail_skips_the_empty_failure_keys(env, monkeypatch):
    # A key the envelope carries EMPTY (no missing outputs recorded, a blank message) names no
    # reason, so it is not rendered — the detail says only what the envelope actually knows.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(
        monkeypatch,
        lambda kw: {
            "status": "stopped",
            "result": {"reply": "half-finished"},
            "error": "",
            "missing_results": [],
            "session_id": "run-quebec",
        },
    )

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.error is not None
    assert "missing_results" not in record.error
    assert "error=" not in record.error
    # The keys that DO carry a value still render.
    assert "stopped" in record.error
    assert "session_id='run-quebec'" in record.error


async def test_tool_target_non_success_detail_is_capped_with_a_truncation_marker(env, monkeypatch):
    # A failure value large enough to bloat the record is clipped — and says so, so a
    # truncated detail never reads as a complete one.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"status": "error", "error": "boom-" + "x" * 10_000})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.error is not None
    assert tool_result_module._FAILED_RESULT_DETAIL_ELLIPSIS in record.error
    # Bounded by the cap plus the surrounding detail wording, nowhere near the 10KB value.
    assert len(record.error) < 2 * tool_result_module._FAILED_RESULT_DETAIL_LIMIT
    # The head of the clipped value survives, so the detail still names the failure.
    assert "boom-" in record.error


async def test_tool_target_non_success_diverts_before_the_reply_mapping(env, monkeypatch):
    # The status is read BEFORE the mapping, not after it: a reply_expr that would FAULT on the
    # partial envelope still yields the status detail. Were the order reversed, the recorded
    # detail would name the mapping fault and the real terminal would be lost.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr='.result.reply | error("mapping ran")')
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"status": "aborted", "result": {"reply": "half-finished"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert record.error.startswith("tool result status 'aborted'")
    assert "reply_expr" not in record.error
    assert "mapping ran" not in record.error


async def test_tool_target_partial_run_envelope_fails_the_turn(env, monkeypatch):
    # The whole composed shape a cut-short run returns, end to end: the terminal it reached,
    # the partial result it did produce, and the outputs it never did. The route's reply_expr
    # maps the SUCCESS shape and would have answered from the partial result.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr='"Your quote is " + (.result.quote.amount | tostring)')
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(
        monkeypatch,
        lambda kw: {
            "status": "aborted",
            "result": {"quote": {"amount": 42}},
            "missing_results": ["send_quote"],
        },
    )

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "aborted" in record.error
    assert "missing_results=['send_quote']" in record.error
    # The half-finished run is NEVER answered as a completed one.
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_target_non_success_uses_the_route_error_reply_text(env, monkeypatch):
    # A non-success terminal is surfaced exactly as any other failed tool run: the route's own
    # participant-facing wording when it carries one.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null", error_reply_text=spanish)
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"status": "aborted", "result": {"reply": "half-finished"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [spanish]


def _paused_envelope(status: str, *, with_missing: bool) -> dict:
    """A paused run-outcome envelope the engine hands a step/resume caller. ``with_missing``
    rides ``missing_results`` (a producer that carries them); without it is the older shape that
    does not. Neither carries a ``result`` — the flagged reply surface is still downstream of the
    pause."""
    envelope: dict = {"status": status, "session_id": "run-sierra"}
    if status == "interrupt":
        envelope["tool_calls"] = [{"tool": "compose_messages", "tool_kwargs": {}}]
    else:
        envelope["interaction_ids"] = ["i-async"]
        envelope["interrupts"] = {"item-1": "i-async"}
        envelope["parks"] = [{"interaction_id": "i-async", "expiry_at": None}]
        envelope["expiry_at"] = None
    if with_missing:
        envelope["missing_results"] = ["compose_messages"]
    return envelope


@pytest.mark.parametrize("with_missing", [True, False])
async def test_tool_target_suspended_envelope_ends_the_turn_silently(env, monkeypatch, with_missing):
    # A SUSPENDED (async re-park) envelope is NEITHER a success nor a failure, and it HAS a
    # delivery leg (the completion continuation bound around the dispatch): the turn ends
    # silently exactly as the SuspendedInteraction marker path does — no reply, no error reply —
    # and the record NOTES the pending state (never delivered). Both producer versions handled:
    # one rides `missing_results`, the older one does not.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: _paused_envelope("suspended", with_missing=with_missing))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    # Silent — the marker path's outcome, not an error outcome.
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer is None
    assert record.answer_status is None
    assert channel.sends == []
    # The record notes the pending state (diagnostic only, never delivered): it names the paused
    # status, and — when the producer rides it — the pending flagged surface.
    assert record.error is not None
    assert "suspended" in record.error
    if with_missing:
        assert "compose_messages" in record.error


@pytest.mark.parametrize("with_missing", [True, False])
async def test_tool_target_interrupt_envelope_fails_the_turn_loudly(env, monkeypatch, with_missing):
    # An INTERRUPT (step-mode tool-call pause) reaching a conversation turn is a permanent route
    # misconfiguration: a step-mode run cannot be driven by a conversation turn, so the pause has
    # NO delivery leg and the reply would never arrive. Silencing it would convert a noticed
    # failure into quiet data loss, so it takes the LOUD error path — the same client-safe error
    # reply a failed run gets — and the record's error names the misconfiguration.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: _paused_envelope("interrupt", with_missing=with_missing))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    # LOUD — the error path, not the silent one.
    assert record.delivery_status is not DeliveryStatus.SILENT
    assert record.answer_status == "error"
    # The participant gets the route's client-safe error reply, never silence.
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    # The record's error names the real cause — the misconfiguration — and the paused status.
    assert record.error is not None
    assert "interrupt" in record.error
    assert "misconfiguration" in record.error


async def test_tool_target_paused_envelope_never_maps_the_authored_reply_guard(env, monkeypatch):
    # The LIVE shape: the flow's reply chain guards its flagged surface (compose_messages) and
    # RAISES when it is empty. On the paused envelope that surface is not present yet. Were the
    # paused envelope mapped as a terminal, reply_expr would run this guard and the turn would
    # FAULT (mapping-failure error path) — the participant getting a "something went wrong" notice for a
    # reply that simply has not committed. The turn must divert BEFORE the mapping and stay silent.
    guard = (
        ".result.outputs.compose_messages as $c "
        '| if ($c // "") == "" '
        'then error("the turn finished without a reply payload (outputs.compose_messages is empty)") '
        "else $c end"
    )
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=guard)
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: _paused_envelope("suspended", with_missing=True))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []
    # The guard never ran: the record carries no reply_expr fault and no participant-facing error.
    assert record.answer_status is None
    assert record.error is not None
    assert "reply_expr" not in record.error
    assert "compose_messages is empty" not in record.error
    # It IS a success terminal, mapped through the SAME guard, that produces the real reply.
    success = {"status": "success", "result": {"outputs": {"compose_messages": "your quote is ready"}}}
    _wire_tool(monkeypatch, lambda kw: success)
    m2 = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi again", "PID2")
    await _settle()
    r2 = await _store().get_record(m2)
    assert r2 is not None
    assert r2.answer == "your quote is ready"
    assert "your quote is ready" in [n.message for n in channel.sends]


async def test_tool_target_mapping_failure_logs_value_free_result_shape(env, monkeypatch, caplog):
    # A complete success terminal whose flagged nodes ride DIRECTLY under `result` (no
    # `result.outputs` level) mapped by a reply_expr reading a stale `.result.outputs.*` path:
    # the guard raises and the turn takes the mapping-failure arm. The diagnostic must name the
    # shape mismatch (which keys exist, which level does not) while NEVER logging a
    # participant-content value.
    guard = (
        ".result.outputs.compose_messages as $c "
        "| if (($c // []) | length) == 0 "
        'then error("the turn finished without a reply payload (outputs.compose_messages is empty)") '
        "else $c end"
    )
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=guard)
    _wire(monkeypatch, FakeManager(route), channel)
    # The real captured envelope: a success terminal, flagged nodes directly under `result`, and a
    # participant reply VALUE that must NEVER reach the log — the value-free proof.
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

    # The turn took the correct disposition: a participant-safe error, the guard's cause recorded.
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "reply_expr" in record.error
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT

    # The permanent shape diagnostic fired for this route...
    shape_logs = [
        r.getMessage()
        for r in caplog.records
        if r.name == _TURN_LOGGER and "had shape:" in r.getMessage() and "tool-line" in r.getMessage()
    ]
    assert len(shape_logs) == 1
    shape = shape_logs[0]
    # ...and it names the decisive structure at once: a SUCCESS terminal whose `result` carries the
    # flagged nodes directly (`compose_messages`, `extract_todo`) with NO `outputs` level — exactly
    # the mismatch against the route's `.result.outputs.compose_messages` path. No inference needed.
    assert "status='success'" in shape
    assert "result_keys=['compose_messages', 'extract_todo']" in shape
    assert "outputs_surface_sizes" not in shape  # there is no result.outputs level to size
    assert "missing_results=['build_state_consent', 'build_state_language']" in shape
    # ...but NO result VALUE ever crosses into the log — not in the shape line, not anywhere.
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
    assert secret not in shape  # the message VALUE never renders, only its surface name + size
    # A non-dict result degrades to a type + length, never a repr of the value.
    assert tool_result_module._result_shape("the reply text") == "type=str len=14"
    assert tool_result_module._result_shape(42) == "type=int"


async def test_tool_target_paused_envelope_binds_completion_and_delivers_on_resume(env, monkeypatch):
    # End-to-end: a paused envelope reaches the turn (a consumer's resume caller kept the raw
    # dict), the turn ends silently, and the generic tool-route completion is bound around the
    # dispatch naming THIS thread — so when the resume drives past the pause to the flagged
    # terminal and fires deliver_tool_completion, the route's reply_expr maps the final result and
    # it is delivered back into the thread. The delivery leg for THIS shape is the same one the
    # SuspendedInteraction park uses (proven adjacent).
    from tai42_contract.interactions import get_park_completion

    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.outputs.compose_messages")
    _wire(monkeypatch, FakeManager(route), channel)

    bound: list = []

    def _pause(kw):
        bound.append(get_park_completion())
        return _paused_envelope("suspended", with_missing=True)

    _wire_tool(monkeypatch, _pause)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []
    # The completion was bound around the paused dispatch, naming this thread + originating route.
    assert len(bound) == 1
    tool_name, context = bound[0]
    assert tool_name == turn_module.DELIVER_TOOL_COMPLETION_NAME
    thread_id = context["delivery_thread_id"]
    assert thread_id == "bridge:tool-line:+15550002222"
    assert context["route_name"] == "tool-line"

    # The resume drives past the pause to compose_messages and fires the completion; reply_expr
    # maps the now-committed reply and it delivers back into the thread.
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


async def test_tool_target_paused_envelope_over_the_api_door_delivers_a_silent_marker(env, monkeypatch):
    # Parity with the SuspendedInteraction api-door path: an api-door tool turn that pauses
    # returns the explicit silent marker through the durable machine (the door promised a
    # callback), never an error notice.
    route = _tool_api_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route))
    _wire_tool(monkeypatch, lambda kw: _paused_envelope("suspended", with_missing=True))

    async def _post(url, body, signature, timeout_seconds):
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


def test_suspended_result_note_names_the_status_and_pending_surfaces():
    # Unit: the silent-arm note names the suspended status, and rides missing_results when the
    # producer carries it — omitting it cleanly when it does not.
    with_missing = tool_result_module._suspended_result_note(
        {"status": "suspended", "missing_results": ["compose_messages"]}
    )
    assert with_missing is not None
    assert "suspended" in with_missing
    assert "compose_messages" in with_missing
    without = tool_result_module._suspended_result_note({"status": "suspended"})
    assert without is not None
    assert "suspended" in without
    assert "compose_messages" not in without
    # An interrupt is NOT a suspend — the silent arm never claims it.
    assert tool_result_module._suspended_result_note({"status": "interrupt"}) is None


def test_interrupt_result_detail_names_the_misconfiguration():
    # Unit: the loud-arm detail names the step-mode interrupt as a route misconfiguration, and
    # rides missing_results when the producer carries it.
    with_missing = tool_result_module._interrupt_result_detail(
        {"status": "interrupt", "missing_results": ["compose_messages"]}
    )
    assert with_missing is not None
    assert "interrupt" in with_missing
    assert "misconfiguration" in with_missing
    assert "compose_messages" in with_missing
    without = tool_result_module._interrupt_result_detail({"status": "interrupt"})
    assert without is not None
    assert "interrupt" in without
    assert "misconfiguration" in without
    assert "compose_messages" not in without
    # A suspend is NOT an interrupt — the loud arm never claims it.
    assert tool_result_module._interrupt_result_detail({"status": "suspended"}) is None


@pytest.mark.parametrize(
    "result",
    [
        # A terminal success is not paused — it maps.
        {"status": "success", "result": {"reply": "done"}, "missing_results": []},
        # A tool's OWN vocabulary that merely reads paused-adjacent stays on the reply path.
        {"status": "queued", "result": {"reply": "done"}},
        {"status": "Suspended", "result": {"reply": "done"}},
        {"status": "Interrupt", "result": {"reply": "done"}},
        # A non-string / non-dict names no paused status.
        {"status": None, "result": {"reply": "done"}},
        {"status": ["suspended"], "result": {"reply": "done"}},
        "a bare string result",
    ],
)
def test_paused_helpers_ignore_non_paused_results(result):
    assert tool_result_module._suspended_result_note(result) is None
    assert tool_result_module._interrupt_result_detail(result) is None
