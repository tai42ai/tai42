"""The one plain message every door reads for a failed terminal, and the capped detail built on it."""

from __future__ import annotations

from tai42_skeleton.interactions.terminal_failure import FAILED_TERMINAL_MESSAGE, failed_outcome_detail


def test_the_plain_message_is_tool_run_failed() -> None:
    assert FAILED_TERMINAL_MESSAGE == "tool run failed"


def test_the_capped_detail_leads_with_the_plain_message() -> None:
    assert failed_outcome_detail({"reason": "stopped"}) == f"{FAILED_TERMINAL_MESSAGE}: {{'reason': 'stopped'}}"
