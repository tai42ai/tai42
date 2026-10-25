"""A preset's output stage: the output map, then the output schema, over the value a preset call returns."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from tai42_contract.interactions import RunFailed
from tai42_contract.presets import PresetOutputStage
from tai42_contract.secrets import SecretValue

from tai42_kit.presets import output_stage
from tai42_kit.presets.output_stage import PresetOutputError, run_output_stage, run_output_stages

_TOKEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"token": {"type": "string", "minLength": 6}},
}


async def test_a_failed_run_passes_untouched() -> None:
    failed = RunFailed(outcome={"status": "failed"}, reply="sorry")
    stage = PresetOutputStage(preset="p", output_map=".nope", output_schema={"type": "string"})
    assert await run_output_stage(stage, failed) is failed


async def test_a_stage_with_nothing_set_returns_the_value() -> None:
    value = {"a": 1}
    assert await run_output_stage(PresetOutputStage(preset="p"), value) is value


async def test_the_map_runs_then_the_schema_checks_the_mapped_value() -> None:
    stage = PresetOutputStage(
        preset="p", output_map="{mapped: .a}", output_schema={"type": "object", "required": ["mapped"]}
    )
    assert await run_output_stage(stage, {"a": 7}) == {"mapped": 7}
    refusing = PresetOutputStage(preset="p", output_map=".a", output_schema={"type": "string"})
    with pytest.raises(PresetOutputError) as caught:
        await run_output_stage(refusing, {"a": 7})
    assert str(caught.value) == (
        "preset 'p' output does not match its output_schema: "
        "value does not match schema at $: 7 is not of type 'string'"
    )
    assert caught.value.preset == "p"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_a_map_over_a_secret_bearing_value_is_refused_before_jq_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("jq ran over a secret")

    monkeypatch.setattr(output_stage, "run_jq_first", _never)
    stage = PresetOutputStage(preset="p", output_map=".token")
    for value, carries in (({"token": SecretValue("tok-secret")}, False), ({"token": "plain-but-revealed"}, True)):
        with pytest.raises(PresetOutputError) as caught:
            await run_output_stage(stage, value, carries_secret=carries)
        assert str(caught.value) == "preset 'p' output map cannot run over a result that carries a secret"
        assert "tok-secret" not in str(caught.value)
        assert "plain-but-revealed" not in str(caught.value)


async def test_a_conforming_secret_bearing_value_is_validated_unwrapped_and_returned_sealed() -> None:
    secret = SecretValue("tok-long-enough")
    value = {"token": secret}
    returned = await run_output_stage(PresetOutputStage(preset="p", output_schema=_TOKEN_SCHEMA), value)
    assert returned is value
    assert returned["token"] is secret


async def test_a_secret_bearing_mismatch_reads_the_value_free_text() -> None:
    stage = PresetOutputStage(preset="p", output_schema=_TOKEN_SCHEMA)
    with pytest.raises(PresetOutputError) as caught:
        await run_output_stage(stage, {"token": SecretValue("tok-x")})
    assert str(caught.value) == (
        "preset 'p' output does not match its output_schema: value does not match schema at $.token: "
        "breaks the schema's 'minLength' rule: 6 (the value is not shown)"
    )
    assert "tok-x" not in str(caught.value)
    assert "tok-x" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_a_revealed_secret_mismatch_reads_the_value_free_text() -> None:
    stage = PresetOutputStage(preset="p", output_schema=_TOKEN_SCHEMA)
    with pytest.raises(PresetOutputError) as caught:
        await run_output_stage(stage, {"token": "tok-x"}, carries_secret=True)
    assert "tok-x" not in str(caught.value)
    assert str(caught.value).endswith("breaks the schema's 'minLength' rule: 6 (the value is not shown)")


async def test_a_plain_mismatch_quotes_the_instance_and_chains_nothing() -> None:
    stage = PresetOutputStage(preset="p", output_schema=_TOKEN_SCHEMA)
    with pytest.raises(PresetOutputError) as caught:
        await run_output_stage(stage, {"token": "tok-x"})
    assert str(caught.value) == (
        "preset 'p' output does not match its output_schema: value does not match schema at $.token: "
        "'tok-x' is too short"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_a_jq_failure_names_the_preset_and_logs_the_jq_text(caplog: pytest.LogCaptureFixture) -> None:
    stage = PresetOutputStage(preset="p", output_map='error("leaked \\(.a)")')
    with caplog.at_level(logging.WARNING), pytest.raises(PresetOutputError) as caught:
        await run_output_stage(stage, {"a": "base-value"})
    assert str(caught.value) == "preset 'p' output map failed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert "base-value" not in str(caught.value)
    assert any("'p'" in record.getMessage() and "base-value" in record.getMessage() for record in caplog.records)


async def test_an_empty_map_output_is_a_failure() -> None:
    with pytest.raises(PresetOutputError, match=r"^preset 'p' output map failed$"):
        await run_output_stage(PresetOutputStage(preset="p", output_map="empty"), {"a": 1})


async def test_a_jq_timeout_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _slow(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("jq evaluation exceeded 10s")

    monkeypatch.setattr(output_stage, "run_jq_first", _slow)
    with pytest.raises(PresetOutputError, match=r"^preset 'p' output map failed$"):
        await run_output_stage(PresetOutputStage(preset="p", output_map="."), {"a": 1})


async def test_the_stages_apply_in_order_and_stop_at_the_first_refusal() -> None:
    inner = PresetOutputStage(preset="inner", output_map="{x: .}")
    outer = PresetOutputStage(preset="outer", output_map="{y: .}")
    assert await run_output_stages([inner, outer], 1) == {"y": {"x": 1}}
    refusing = PresetOutputStage(preset="mid", output_schema={"type": "string"})
    never = PresetOutputStage(preset="never", output_map='error("ran")')
    with pytest.raises(PresetOutputError) as caught:
        await run_output_stages([inner, refusing, never], 1)
    assert caught.value.preset == "mid"
    assert await run_output_stages([], {"a": 1}) == {"a": 1}


def test_the_error_keeps_its_preset_beside_the_message() -> None:
    error = PresetOutputError("preset 'p' output map failed", preset="p")
    assert error.preset == "p"
    assert str(error) == "preset 'p' output map failed"
