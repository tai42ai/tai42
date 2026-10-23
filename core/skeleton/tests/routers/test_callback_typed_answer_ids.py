"""The callback door's channel-typed answer body: the medium ids a channel sends beside the answer."""

from __future__ import annotations

import json

import pytest
from starlette.responses import Response
from tai42_contract.interactions import AnswerFormat


def _parse(body: dict):
    from tai42_skeleton.routers.interactions.callback import _parse_typed_answer

    return _parse_typed_answer(json.dumps(body).encode(), AnswerFormat.TEXT)


def test_the_body_carries_the_quoted_message_id_beside_the_answer() -> None:
    parsed = _parse({"answer": "yes", "medium_message_id": "M-2", "reply_to_medium_message_id": "M-1"})
    assert not isinstance(parsed, Response)
    assert parsed.answer == "yes"
    assert parsed.medium_message_id == "M-2"
    assert parsed.reply_to_medium_message_id == "M-1"


def test_a_body_quoting_nothing_carries_no_quoted_message_id() -> None:
    parsed = _parse({"answer": "yes"})
    assert not isinstance(parsed, Response)
    assert parsed.reply_to_medium_message_id is None
    assert parsed.medium_message_id is None
    assert parsed.params is None


@pytest.mark.parametrize("value", ["", "   ", 42, ["M-1"], {"id": "M-1"}])
def test_a_quoted_message_id_that_is_not_a_non_blank_string_is_a_400(value) -> None:
    parsed = _parse({"answer": "yes", "reply_to_medium_message_id": value})
    assert isinstance(parsed, Response)
    assert parsed.status_code == 400
    assert json.loads(bytes(parsed.body)) == {"error": "reply_to_medium_message_id must be a non-blank string"}
