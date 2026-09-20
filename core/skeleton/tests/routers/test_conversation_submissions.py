"""Unit tests for the shared turn-submission seam the message and event doors run:
the exception-type -> HTTP-status table, the mapping helper, and the ack shaper."""

from __future__ import annotations

import json

from tai42_contract.conversations import ConversationAnswer

from tai42_skeleton.conversations.caps import AddressRateLimitedError, ThreadQueueOverflowError
from tai42_skeleton.conversations.turn import (
    ApiSubmitResult,
    ConversationRouteResolutionError,
    EventTargetNotToolError,
    ThreadNotFoundError,
)
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.routers.conversations.submissions import (
    _EVENT_SUBMISSION_STATUS,
    _TURN_SUBMISSION_STATUS,
    _turn_ack_response,
    _turn_submission_error,
)


def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


def test_turn_status_table_maps_each_failure_to_its_status():
    assert {
        ConversationRouteResolutionError: 404,
        AddressRateLimitedError: 429,
        ThreadQueueOverflowError: 503,
        NotSupportedError: 501,
    } == _TURN_SUBMISSION_STATUS


def test_event_status_table_adds_the_two_event_only_terminals():
    # The event door extends the base table with its own two terminals; every base
    # mapping is preserved.
    assert _EVENT_SUBMISSION_STATUS[ThreadNotFoundError] == 404
    assert _EVENT_SUBMISSION_STATUS[EventTargetNotToolError] == 409
    for exc_type, status in _TURN_SUBMISSION_STATUS.items():
        assert _EVENT_SUBMISSION_STATUS[exc_type] == status


def test_turn_submission_error_maps_a_known_failure():
    resp = _turn_submission_error(AddressRateLimitedError("slow down"), _TURN_SUBMISSION_STATUS)
    assert resp is not None
    assert resp.status_code == 429
    assert _body(resp) == {"error": "slow down"}


def test_turn_submission_error_maps_an_event_only_failure():
    resp = _turn_submission_error(EventTargetNotToolError("not a tool"), _EVENT_SUBMISSION_STATUS)
    assert resp is not None
    assert resp.status_code == 409


def test_turn_submission_error_returns_none_for_an_unmapped_failure():
    # An unmapped failure is NOT swallowed here — the caller re-raises it.
    assert _turn_submission_error(RuntimeError("boom"), _TURN_SUBMISSION_STATUS) is None


def test_turn_ack_response_202_when_no_answer_yet():
    result = ApiSubmitResult(message_id="m1", thread_id="t1", answer=None)
    resp = _turn_ack_response(result)
    assert resp.status_code == 202
    assert _body(resp) == {"data": {"message_id": "m1", "thread_id": "t1"}}


def test_turn_ack_response_200_with_the_answer_when_present():
    answer = ConversationAnswer(message_id="m2", thread_id="t2", status="answered", answer="hi")
    result = ApiSubmitResult(message_id="m2", thread_id="t2", answer=answer)
    resp = _turn_ack_response(result)
    assert resp.status_code == 200
    # ``exclude_none`` drops the answer's own null fields (``parts``), never the outer key.
    assert _body(resp) == {
        "data": {
            "message_id": "m2",
            "thread_id": "t2",
            "answer": {"message_id": "m2", "thread_id": "t2", "status": "answered", "answer": "hi"},
        }
    }
