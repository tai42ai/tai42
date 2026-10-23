"""Tests for the quoted-message model ``ReplyTo`` and the synchronous ask value ``AskAnswer``."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from tai42_contract.conversations import ReplyTo, ReplyToStatus
from tai42_contract.interactions import ELICIT_META_REPLY_TO, AskAnswer
from tai42_contract.interactions.models import InteractionResponse
from tai42_contract.secrets import SecretValue

_SECRET = "s3cr3t-value-never-shown"


def test_reply_to_found_requires_non_blank_text():
    quoted = ReplyTo(message_id="m-1", status=ReplyToStatus.FOUND, text="the earlier message")
    assert quoted.text == "the earlier message"
    for text in (None, "", "   "):
        with pytest.raises(ValidationError, match="text"):
            ReplyTo(message_id="m-1", status=ReplyToStatus.FOUND, text=text)


@pytest.mark.parametrize("status", [ReplyToStatus.NOT_FOUND, ReplyToStatus.SENSITIVE, ReplyToStatus.NO_TEXT])
def test_reply_to_other_statuses_refuse_a_text(status: ReplyToStatus):
    assert ReplyTo(message_id="m-1", status=status, text=None).text is None
    with pytest.raises(ValidationError, match="text"):
        ReplyTo(message_id="m-1", status=status, text="something")


@pytest.mark.parametrize("message_id", ["", "   "])
def test_reply_to_refuses_a_blank_message_id(message_id: str):
    with pytest.raises(ValidationError, match="message_id"):
        ReplyTo(message_id=message_id, status=ReplyToStatus.NOT_FOUND, text=None)


def test_reply_to_refuses_unknown_keys_and_is_frozen():
    with pytest.raises(ValidationError):
        ReplyTo.model_validate({"message_id": "m-1", "status": "not_found", "text": None, "extra": 1})
    quoted = ReplyTo(message_id="m-1", status=ReplyToStatus.NOT_FOUND, text=None)
    with pytest.raises(ValidationError):
        quoted.message_id = "m-2"  # type: ignore[misc]


def test_reply_to_round_trips_through_json():
    quoted = ReplyTo(message_id="m-1", status=ReplyToStatus.FOUND, text="hello")
    assert quoted.model_dump(mode="json") == {"message_id": "m-1", "status": "found", "text": "hello"}
    assert ReplyTo.model_validate_json(quoted.model_dump_json()) == quoted


def test_ask_answer_holds_any_answer_a_reply_to_and_params():
    quoted = ReplyTo(message_id="m-1", status=ReplyToStatus.FOUND, text="hello")
    value = AskAnswer(answer={"choice": "b"}, reply_to=quoted, params={"button": "b"})
    assert value.answer == {"choice": "b"}
    assert value.reply_to == quoted
    assert value.params == {"button": "b"}
    bare = AskAnswer(answer="yes", reply_to=None, params=None)
    assert bare.reply_to is None
    assert bare.params is None


def test_ask_answer_requires_every_field_and_refuses_unknown_keys():
    with pytest.raises(ValidationError, match="reply_to"):
        AskAnswer.model_validate({"answer": "yes", "params": None})
    with pytest.raises(ValidationError, match="params"):
        AskAnswer.model_validate({"answer": "yes", "reply_to": None})
    with pytest.raises(ValidationError):
        AskAnswer.model_validate({"answer": "yes", "reply_to": None, "params": None, "extra": 1})


def test_ask_answer_bounds_params_by_the_entry_params_vocabulary():
    with pytest.raises(ValidationError):
        AskAnswer(answer="yes", reply_to=None, params={"": "x"})


def test_ask_answer_round_trips_through_json():
    quoted = ReplyTo(message_id="m-1", status=ReplyToStatus.SENSITIVE, text=None)
    value = AskAnswer(answer=[1, "two"], reply_to=quoted, params={"k": "v"})
    dumped = value.model_dump(mode="json")
    assert dumped == {
        "answer": [1, "two"],
        "reply_to": {"message_id": "m-1", "status": "sensitive", "text": None},
        "params": {"k": "v"},
    }
    assert AskAnswer.model_validate_json(value.model_dump_json()) == value


def test_ask_answer_keeps_a_sensitive_answer_protected():
    secret = SecretValue(_SECRET)
    value = AskAnswer(answer=secret, reply_to=None, params=None)
    assert value.answer is secret
    assert _SECRET not in repr(value)
    assert "SecretValue" in repr(value)
    assert value.model_dump()["answer"] is secret
    with pytest.raises(PydanticSerializationError) as json_dump:
        value.model_dump(mode="json")
    assert _SECRET not in str(json_dump.value)
    with pytest.raises(PydanticSerializationError) as json_text:
        value.model_dump_json()
    assert _SECRET not in str(json_text.value)
    with pytest.raises(TypeError) as deep:
        copy.deepcopy(value)
    assert _SECRET not in str(deep.value)
    assert value.answer.reveal() == _SECRET


def test_elicit_meta_reply_to_key():
    import tai42_contract.interactions as interactions

    assert ELICIT_META_REPLY_TO == "tai42/reply_to"
    assert "ELICIT_META_REPLY_TO" in interactions.__all__
    assert "AskAnswer" in interactions.__all__


def test_interaction_response_round_trips_the_quoted_medium_message_id():
    response = InteractionResponse(
        interaction_id="i-1",
        answer="yes",
        answered_by="channel",
        answered_at=datetime(2026, 1, 1, tzinfo=UTC),
        reply_to_medium_message_id="wamid.1",
    )
    assert InteractionResponse.model_validate_json(response.model_dump_json()) == response
    assert response.reply_to_medium_message_id == "wamid.1"
    plain = InteractionResponse(
        interaction_id="i-1", answer="yes", answered_by="channel", answered_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert plain.reply_to_medium_message_id is None
