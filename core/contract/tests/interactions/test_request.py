"""Validator tests for the durable ``InteractionRequest`` — per-answer-format payload,
the async park discipline, ``SuspendedInteraction``, the ``check_ask_timing`` guard and the
``AskUser`` signature, plus naive-datetime rejection."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from tai42_contract.interactions.asker import AskUser, check_ask_timing
from tai42_contract.interactions.models import (
    QUESTION_MAX_CHARS,
    AnswerFormat,
    InteractionRequest,
    SuspendedInteraction,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _interaction(**overrides: Any) -> InteractionRequest:
    base: dict[str, Any] = {
        "interaction_id": "i1",
        "group_id": "g1",
        "question": "?",
        "reply_to": "ch",
        "created_at": _now(),
        "timeout_at": _now(),
    }
    base.update(overrides)
    return InteractionRequest(**base)


def test_interaction_select_valid():
    req = _interaction(answer_format=AnswerFormat.SELECT, format_payload={"options": ["a", "b"]})
    assert req.answer_format is AnswerFormat.SELECT


def test_interaction_select_requires_options():
    with pytest.raises(ValueError, match="non-empty options"):
        _interaction(answer_format=AnswerFormat.SELECT, format_payload={"options": []})
    with pytest.raises(ValueError, match="non-empty options"):
        _interaction(answer_format=AnswerFormat.SELECT, format_payload=None)


def test_interaction_form_valid():
    req = _interaction(answer_format=AnswerFormat.FORM, format_payload={"schema": {"type": "object"}})
    assert req.answer_format is AnswerFormat.FORM


def test_interaction_form_requires_schema():
    with pytest.raises(ValueError, match="requires a schema"):
        _interaction(answer_format=AnswerFormat.FORM, format_payload={})


def test_interaction_question_at_cap_accepted():
    at_cap = "q" * QUESTION_MAX_CHARS
    assert _interaction(question=at_cap).question == at_cap


def test_interaction_question_over_cap_raises():
    # The question is stored verbatim into the state hash + group stream, so an
    # over-cap value is refused loudly rather than persisted unbounded.
    with pytest.raises(ValueError, match=f"question must be at most {QUESTION_MAX_CHARS}"):
        _interaction(question="q" * (QUESTION_MAX_CHARS + 1))


def test_interaction_text_valid_no_payload():
    assert _interaction(answer_format=AnswerFormat.TEXT).format_payload is None


def test_interaction_text_forbids_non_options_payload():
    # TEXT carries no payload EXCEPT an optional ``options`` list; any other key is refused.
    with pytest.raises(ValueError, match="carries only optional options"):
        _interaction(answer_format=AnswerFormat.TEXT, format_payload={"x": 1})


def test_interaction_text_allows_suggested_reply_options():
    # TEXT MAY carry ``options`` as suggested replies — a tap submits the option's own text
    # as the free-text answer (which stays unconstrained), unlike SELECT's required set.
    req = _interaction(answer_format=AnswerFormat.TEXT, format_payload={"options": ["ok", "later"]})
    assert req.format_payload == {"options": ["ok", "later"]}


def test_interaction_text_options_must_be_non_empty_when_present():
    with pytest.raises(ValueError, match="non-empty list when present"):
        _interaction(answer_format=AnswerFormat.TEXT, format_payload={"options": []})


def test_interaction_confirm_forbids_payload():
    with pytest.raises(ValueError, match="carries no format_payload"):
        _interaction(answer_format=AnswerFormat.CONFIRM, format_payload={"x": 1})


def test_interaction_external_valid():
    req = _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={"url": "https://sign.example/abc"})
    assert req.answer_format is AnswerFormat.EXTERNAL


def test_interaction_external_requires_url():
    # Missing, empty, and non-str url are all rejected.
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={})
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={"url": ""})
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload={"url": 123})
    with pytest.raises(ValueError, match="non-empty string url"):
        _interaction(answer_format=AnswerFormat.EXTERNAL, format_payload=None)


def test_interaction_channel_defaults_none_and_round_trips():
    assert _interaction().channel is None
    req = _interaction(channel="telegram")
    assert req.channel == "telegram"
    assert InteractionRequest.model_validate(req.model_dump()).channel == "telegram"


def test_interaction_audience_defaults_none_and_json_round_trips():
    # Omitting audience yields None, and that None survives a
    # model_dump_json/model_validate_json round-trip (the store persists via
    # model_dump_json).
    assert _interaction().audience is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.audience is None


def test_interaction_audience_explicit_json_round_trips():
    # An explicit audience user_id serializes to JSON and reloads unchanged.
    req = _interaction(audience="user-42")
    assert req.audience == "user-42"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.audience == "user-42"


def test_interaction_recipient_defaults_none_and_json_round_trips():
    # Omitting recipient yields None, surviving the store's model_dump_json round-trip.
    assert _interaction().recipient is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.recipient is None


def test_interaction_recipient_explicit_json_round_trips():
    # An explicit delivery address serializes to JSON and reloads unchanged.
    req = _interaction(recipient="+15551234567")
    assert req.recipient == "+15551234567"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.recipient == "+15551234567"


def test_interaction_origin_defaults_none_and_json_round_trips():
    # Omitting origin yields None, surviving the store's model_dump_json round-trip.
    assert _interaction().origin is None
    default = InteractionRequest.model_validate_json(_interaction().model_dump_json())
    assert default.origin is None


def test_interaction_origin_explicit_json_round_trips():
    # An explicit run origin serializes to JSON and reloads unchanged.
    req = _interaction(origin="run-abc123")
    assert req.origin == "run-abc123"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.origin == "run-abc123"


# === interactions/models.py — InteractionRequest async park =================


def test_interaction_sync_default_carries_no_async_fields():
    # The default mode is sync, and no continuation fields or expiry are set: every
    # existing sync caller stays valid without touching the async surface.
    req = _interaction()
    assert req.mode == "sync"
    assert req.continuation_tool is None
    assert req.continuation_identity is None
    assert req.expiry_at is None


def test_interaction_async_with_continuation_valid_and_json_round_trips():
    req = _interaction(
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key-1",
        expiry_at=_now(),
    )
    assert req.mode == "async"
    assert req.continuation_tool == "resume_tool"
    assert req.continuation_identity == "svc-key-1"
    restored = InteractionRequest.model_validate_json(req.model_dump_json())
    assert restored.mode == "async"
    assert restored.continuation_tool == "resume_tool"
    assert restored.continuation_identity == "svc-key-1"
    assert restored.expiry_at == req.expiry_at


def test_interaction_async_missing_continuation_tool_raises_naming_it():
    with pytest.raises(ValueError, match="continuation_tool"):
        _interaction(mode="async", continuation_identity="svc-key-1")


def test_interaction_async_missing_continuation_identity_raises_naming_it():
    with pytest.raises(ValueError, match="continuation_identity"):
        _interaction(mode="async", continuation_tool="resume_tool")


def test_interaction_sync_with_continuation_tool_raises_naming_it():
    with pytest.raises(ValueError, match="continuation_tool"):
        _interaction(continuation_tool="resume_tool")


def test_interaction_sync_with_continuation_identity_raises_naming_it():
    with pytest.raises(ValueError, match="continuation_identity"):
        _interaction(continuation_identity="svc-key-1")


# === interactions/models.py — SuspendedInteraction ==========================


def test_suspended_interaction_constructs_and_json_round_trips():
    s = SuspendedInteraction(interaction_id="i1", expiry_at=_now())
    assert s.interaction_id == "i1"
    restored = SuspendedInteraction.model_validate_json(s.model_dump_json())
    assert restored.interaction_id == "i1"
    assert restored.expiry_at == s.expiry_at


def test_suspended_interaction_expiry_defaults_none():
    assert SuspendedInteraction(interaction_id="i1").expiry_at is None


def test_suspended_interaction_requires_interaction_id():
    with pytest.raises(ValueError, match="interaction_id"):
        SuspendedInteraction()  # type: ignore[call-arg]


def test_suspended_interaction_rejects_naive_expiry_at():
    # A naive ``expiry_at`` would raise TypeError when compared against an aware
    # ``now()`` by the expiry reaper; the validator refuses it at construction.
    with pytest.raises(ValueError, match="timezone-aware"):
        SuspendedInteraction(interaction_id="i1", expiry_at=datetime(2026, 1, 1, 12, 0))


def test_suspended_interaction_expiry_at_normalized_to_utc():
    plus2 = timezone(timedelta(hours=2))
    s = SuspendedInteraction(
        interaction_id="i1",
        expiry_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus2),
    )
    assert s.expiry_at is not None
    assert s.expiry_at.tzinfo == UTC
    assert s.expiry_at.hour == 10


# === interactions/asker.py — check_ask_timing + AskUser signature ===========


def test_check_ask_timing_allows_timeout_only():
    check_ask_timing(timeout=5.0, expiry_at=None)


def test_check_ask_timing_allows_expiry_only():
    check_ask_timing(timeout=None, expiry_at=_now())


def test_check_ask_timing_allows_neither():
    check_ask_timing(timeout=None, expiry_at=None)


def test_check_ask_timing_rejects_both():
    with pytest.raises(ValueError, match="mutually exclusive"):
        check_ask_timing(timeout=5.0, expiry_at=_now())


def test_ask_user_signature_has_keyword_only_mode_and_expiry():
    params = inspect.signature(AskUser.__call__).parameters
    assert params["mode"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["mode"].default == "sync"
    assert params["expiry_at"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["expiry_at"].default is None


# === interactions — InteractionRequest naive-datetime rejection =============


def test_interaction_rejects_naive_created_at():
    with pytest.raises(ValueError, match="timezone-aware"):
        _interaction(created_at=datetime(2026, 1, 1))


def test_interaction_rejects_naive_timeout_at():
    with pytest.raises(ValueError, match="timezone-aware"):
        _interaction(timeout_at=datetime(2026, 1, 1))


def test_interaction_datetime_normalized_to_utc():
    plus2 = timezone(timedelta(hours=2))
    req = _interaction(created_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus2))
    assert req.created_at.tzinfo == UTC
    assert req.created_at.hour == 10


def test_interaction_rejects_naive_expiry_at():
    with pytest.raises(ValueError, match="timezone-aware"):
        _interaction(mode="async", continuation_tool="t", continuation_identity="k", expiry_at=datetime(2026, 1, 1))


def test_interaction_async_missing_expiry_at_raises_naming_it():
    # An async park without an ``expiry_at`` is never expiry-indexed, so the reaper
    # could never fire its continuation — the validator refuses it, naming the field.
    with pytest.raises(ValueError, match="expiry_at"):
        _interaction(mode="async", continuation_tool="t", continuation_identity="k")


def test_interaction_expiry_at_normalized_to_utc():
    plus2 = timezone(timedelta(hours=2))
    req = _interaction(
        mode="async",
        continuation_tool="t",
        continuation_identity="k",
        expiry_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus2),
    )
    assert req.expiry_at is not None
    assert req.expiry_at.tzinfo == UTC
    assert req.expiry_at.hour == 10
