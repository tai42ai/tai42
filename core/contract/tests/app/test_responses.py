"""The profile-apply response models: a recycle line carries an optional ``detail``."""

from __future__ import annotations

from tai42_contract.app.responses import ProfileApplyResponse, RecycleEntry, RecycleStop


def test_a_recycle_line_round_trips_with_and_without_a_detail() -> None:
    recycled = RecycleEntry(name="backend-1", kind="backend", status="recycled", generation_before=1)
    assert recycled.detail is None
    assert RecycleEntry.model_validate(recycled.model_dump()) == recycled

    stopped = RecycleEntry(
        name="backend-2", kind="backend", status="timed-out", generation_before=3, detail="old life still present"
    )
    assert RecycleEntry.model_validate_json(stopped.model_dump_json()) == stopped


def test_a_recycle_line_without_a_detail_key_validates() -> None:
    entry = RecycleEntry.model_validate(
        {"name": "serve-1", "kind": "serve", "status": "self-deferred", "generation_before": 7}
    )
    assert entry.detail is None


def test_the_detail_is_optional_in_the_schema() -> None:
    schema = ProfileApplyResponse.model_json_schema()["$defs"]["RecycleEntry"]
    assert "detail" in schema["properties"]
    assert "detail" not in schema["required"]


def test_a_recycle_stop_round_trips_with_and_without_a_target() -> None:
    no_target = RecycleStop(kind="backend", name=None, detail="bus unreachable while reading the census")
    assert RecycleStop.model_validate_json(no_target.model_dump_json()) == no_target
    with_target = RecycleStop(kind="backend", name="backend-1", detail="old life still present")
    assert RecycleStop.model_validate(with_target.model_dump()) == with_target


def test_the_profile_apply_response_defaults_recycle_stopped_to_none() -> None:
    body = ProfileApplyResponse.model_validate(
        {"hot": [], "recycle": [], "fresh": [], "refused": [], "fanout": {"mode": "fleet"}}
    )
    assert body.recycle_stopped is None
    schema = ProfileApplyResponse.model_json_schema()
    assert "recycle_stopped" in schema["properties"]
    assert "recycle_stopped" not in schema["required"]
