"""The ``claude_code`` structured result is validated against the authored schema.

The Claude Code CLI runtime emits its structured verdict with no in-loop retry seam, so the
terminal validates the result against the requested ``response_format`` with the same
``validate_structured_output`` every other door uses and raises LOUDLY on a non-conforming
result — a bad structured verdict is a hard, visible failure, never a silent pass.
"""

from __future__ import annotations

import pytest
from tai42_contract.agent.events import StructuredFinal
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents.claude_code.frames import terminal_event
from tai42_agents.claude_code.protocol import ResultFrame

_SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"value": {"type": "integer", "minimum": 0}},
    "required": ["value"],
}


def _result(payload: object) -> ResultFrame:
    return ResultFrame(terminal_reason="success", result=payload, is_structured=True)


def test_conforming_structured_result_passes() -> None:
    event = terminal_event(_result({"value": 7}), [], response_format=_SCHEMA)
    assert isinstance(event, StructuredFinal)
    assert event.data == {"value": 7}


def test_nonconforming_structured_result_raises_loudly() -> None:
    with pytest.raises(JsonSchemaValidationError):
        terminal_event(_result({"value": -1}), [], response_format=_SCHEMA)


def test_oversized_int_structured_result_raises_loudly() -> None:
    with pytest.raises(JsonSchemaValidationError):
        terminal_event(_result({"value": 9223372036854775808}), [], response_format=_SCHEMA)


def test_no_response_format_skips_validation() -> None:
    # With no requested response_format the structured result passes through unvalidated
    # (the caller asked for none), as before.
    event = terminal_event(_result({"anything": True}), [])
    assert isinstance(event, StructuredFinal)
    assert event.data == {"anything": True}
