"""The preset map fields, the output stage shapes, the failed outcome and the park's stage fields."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from tai42_contract.interactions import InteractionRequest, SuspendedInteraction
from tai42_contract.presets import (
    PresetBody,
    PresetOutputStage,
    PresetParkStages,
    PresetSeed,
    preset_output_failed_outcome,
)
from tai42_contract.template import TemplatedText


def test_the_body_maps_round_trip_as_text_or_templated_text():
    body = PresetBody(
        base_tool="weather",
        description="d",
        input_schema={"type": "object"},
        input_map="{city: .where}",
        output_map=TemplatedText(content=".temp"),
    )
    again = PresetBody.model_validate_json(body.model_dump_json())
    assert again == body
    assert again.input_map == "{city: .where}"
    assert again.output_map == TemplatedText(content=".temp")


def test_a_body_without_maps_validates_with_both_unset():
    body = PresetBody.model_validate({"base_tool": "weather", "description": "d"})
    assert body.input_map is None
    assert body.output_map is None


def test_the_seed_carries_both_maps():
    seed = PresetSeed(
        name="p",
        description="d",
        base_tool="weather",
        input_map=".",
        output_map=TemplatedText(id="out-map"),
    )
    assert PresetSeed.model_validate(seed.model_dump()) == seed


def test_an_output_stage_is_frozen_and_refuses_an_unknown_key():
    stage = PresetOutputStage(preset="p", output_map=".a", output_schema={"type": "object"})
    with pytest.raises(ValidationError):
        stage.preset = "q"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        PresetOutputStage.model_validate({"preset": "p", "extra": 1})
    assert PresetOutputStage(preset="p") == PresetOutputStage(preset="p", output_map=None, output_schema=None)


def test_park_stages_default_empty_and_round_trip():
    empty = PresetParkStages()
    assert (empty.answer, empty.terminal, empty.chained) == ([], [], {})
    stages = PresetParkStages(
        answer=[PresetOutputStage(preset="a")],
        terminal=[PresetOutputStage(preset="t", output_map=".x")],
        chained={"tai42:chained-park:k": [PresetOutputStage(preset="c", output_schema={"type": "string"})]},
    )
    assert PresetParkStages.model_validate_json(stages.model_dump_json()) == stages
    with pytest.raises(ValidationError):
        PresetParkStages.model_validate({"answer": [], "other": []})


def test_the_failed_outcome_names_the_preset_and_the_message():
    assert preset_output_failed_outcome("p", "preset 'p' output map failed") == {
        "tai42:preset_output_failed": True,
        "preset": "p",
        "message": "preset 'p' output map failed",
    }


def _request(**overrides: Any) -> InteractionRequest:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "interaction_id": "i1",
        "group_id": "g1",
        "question": "?",
        "reply_to": "ch",
        "created_at": now,
        "timeout_at": now,
    }
    base.update(overrides)
    return InteractionRequest(**base)


def test_the_request_carries_the_deferred_output_stages():
    assert _request().deferred_output_stages is None
    stages = PresetParkStages(
        terminal=[PresetOutputStage(preset="t", output_map=".x")],
        chained={"tai42:chained-park:k": [PresetOutputStage(preset="c")]},
    )
    request = _request(deferred_output_stages=stages)
    again = InteractionRequest.model_validate_json(request.model_dump_json())
    assert again.deferred_output_stages == stages


def test_the_suspended_output_stages_never_dump():
    stage = PresetOutputStage(preset="p", output_map=".a")
    park = SuspendedInteraction(interaction_id="i-1", output_stages=[stage])
    assert park.output_stages == [stage]
    assert "output_stages" not in park.model_dump()
    assert "output_stages" not in park.model_dump(mode="json")
    assert "output_stages" not in park.model_dump_json()
    assert SuspendedInteraction(interaction_id="i-1").output_stages == []
