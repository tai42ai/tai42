"""The kit classifier contract: each question kind validates, answers round-trip, bounds are enforced.

These models are the platform's classifier contract (JSON in / JSON out). The
tests prove its field constraints: the three discriminated question kinds, a
non-empty request, bounded probabilities, and a clean vendor-shaped response
round trip.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_kit.llm.classifier import (
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifyAnswer,
    ClassifyQuestion,
    ClassifyRequest,
    ClassifyResponse,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)

_question_adapter = TypeAdapter(ClassifyQuestion)
_answer_adapter = TypeAdapter(ClassifyAnswer)


def test_noul_question_validates_from_json_with_optional_criteria():
    q = _question_adapter.validate_python(
        {"type": "noul", "instructions": "Is this an alert?", "criteria": {"true": "yes", "false": "no"}}
    )
    assert isinstance(q, NoulQuestion)
    assert q.criteria is not None
    assert q.criteria.true == "yes"


def test_noul_question_criteria_optional():
    q = _question_adapter.validate_python({"type": "noul", "instructions": "Is this an alert?"})
    assert isinstance(q, NoulQuestion)
    assert q.criteria is None


def test_choice_question_validates_and_requires_a_label():
    q = _question_adapter.validate_python(
        {"type": "choice", "instructions": "Which lane?", "criteria": {"events": "an event", "status": "a status"}}
    )
    assert isinstance(q, ChoiceQuestion)
    assert set(q.criteria) == {"events", "status"}


def test_choice_question_rejects_empty_criteria():
    with pytest.raises(ValidationError):
        _question_adapter.validate_python({"type": "choice", "instructions": "Which lane?", "criteria": {}})


def test_score_question_validates_ordered_levels():
    q = _question_adapter.validate_python(
        {"type": "score", "instructions": "How severe?", "criteria": ["low", "medium", "high"]}
    )
    assert isinstance(q, ScoreQuestion)
    assert q.criteria == ["low", "medium", "high"]


def test_score_question_rejects_single_level():
    with pytest.raises(ValidationError):
        _question_adapter.validate_python({"type": "score", "instructions": "How severe?", "criteria": ["only"]})


def test_question_rejects_unknown_type():
    with pytest.raises(ValidationError):
        _question_adapter.validate_python({"type": "bogus", "instructions": "x"})


def test_question_requires_instructions():
    with pytest.raises(ValidationError):
        _question_adapter.validate_python({"type": "noul"})


def test_request_requires_at_least_one_question():
    with pytest.raises(ValidationError):
        ClassifyRequest(state={"upstream": 1}, questions={})


def test_request_accepts_json_state_and_questions():
    request = ClassifyRequest(
        state={"events": ["a", "b"]},
        questions={"flag": NoulQuestion(instructions="Is this an alert?")},
    )
    assert request.state == {"events": ["a", "b"]}
    assert isinstance(request.questions["flag"], NoulQuestion)


def test_answers_discriminate_on_type():
    assert isinstance(_answer_adapter.validate_python({"type": "noul", "noul": 0.5}), NoulAnswer)
    assert isinstance(
        _answer_adapter.validate_python(
            {"type": "choice", "choice": "events", "probabilities": {"events": 1.0}, "confidence": 0.9}
        ),
        ChoiceAnswer,
    )
    assert isinstance(
        _answer_adapter.validate_python(
            {
                "type": "score",
                "score": 1.2,
                "legend": {0: "low", 1: "high"},
                "probabilities": {0: 0.4, 1: 0.6},
                "confidence": 0.7,
            }
        ),
        ScoreAnswer,
    )


def test_noul_answer_out_of_range_raises():
    with pytest.raises(ValidationError):
        NoulAnswer(type="noul", noul=1.5)


def test_choice_answer_confidence_out_of_range_raises():
    with pytest.raises(ValidationError):
        ChoiceAnswer(type="choice", choice="events", probabilities={"events": 1.0}, confidence=1.2)


def test_response_round_trips_a_vendor_shaped_body():
    body = {
        "model": "jev-latest",
        "answers": {
            "flag": {"type": "noul", "noul": 0.8},
            "lane": {
                "type": "choice",
                "choice": "events",
                "probabilities": {"events": 0.7, "status": 0.3},
                "confidence": 0.6,
            },
            "severity": {
                "type": "score",
                "score": 1.4,
                "legend": {"0": "low", "1": "high"},
                "probabilities": {"0": 0.3, "1": 0.7},
                "confidence": 0.55,
            },
        },
        "usage": {"input_tokens": 12, "output_tokens": 4},
        "request_id": "req-123",
    }
    response = ClassifyResponse.model_validate(body)
    assert response.model == "jev-latest"
    assert isinstance(response.answers["flag"], NoulAnswer)
    assert isinstance(response.answers["lane"], ChoiceAnswer)
    assert isinstance(response.answers["severity"], ScoreAnswer)
    # String level keys from a JSON body coerce back to the int-keyed legend/distribution.
    assert response.answers["severity"].legend[0] == "low"
    assert response.answers["severity"].probabilities[1] == pytest.approx(0.7)
    assert response.usage.input_tokens == 12
    assert response.request_id == "req-123"

    # A JSON dump re-validates to an equal response.
    assert ClassifyResponse.model_validate(response.model_dump(mode="json")) == response


def test_response_defaults_usage_and_request_id():
    response = ClassifyResponse.model_validate(
        {"model": "jev-latest", "answers": {"flag": {"type": "noul", "noul": 0.1}}}
    )
    assert response.usage.input_tokens is None
    assert response.request_id is None
