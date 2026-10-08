"""The terminal chain notice: one payload shape for a success and for a failed terminal."""

from __future__ import annotations

from tai42_contract.interactions import (
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    ChainedResume,
    RunFailed,
)

from tai42_kit.interactions.park_adoption import terminal_chain_notice

_ROUTING = ChainedResume(delivery_tool="probe_chain_deliver", chain_key="k-1", asked_by=("caller",))


def test_a_success_carries_the_result_whole() -> None:
    tool, payload = terminal_chain_notice(_ROUTING, {"answer": [1, 2]})

    assert tool == "probe_chain_deliver"
    assert payload == {CHAINED_PARK_TOKEN_KEY: "k-1", "result": {"answer": [1, 2]}, "status": PARK_COMPLETION_SUCCEEDED}


def test_a_failed_terminal_carries_its_outcome_under_the_failed_status() -> None:
    tool, payload = terminal_chain_notice(_ROUTING, RunFailed(outcome={"status": "error", "error": "down"}))

    assert tool == "probe_chain_deliver"
    assert set(payload) == {CHAINED_PARK_TOKEN_KEY, "result", "status"}
    assert payload["result"] == {"status": "error", "error": "down"}
    assert payload["status"] == PARK_COMPLETION_FAILED
