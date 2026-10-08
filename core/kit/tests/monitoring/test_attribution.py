"""Attribution frames: the merge rules and the span processor that stamps them."""

from __future__ import annotations

from typing import Any

import orjson
from tai42_contract.monitoring import RUN_VERSION_METADATA_KEY

from tai42_kit.monitoring.encode import encode_payload
from tai42_kit.monitoring.otel import attributes as attr
from tai42_kit.monitoring.otel.attribution import AttributionFrame, merged_attributes, pushed_frame


def _encode(value: Any, _record: str) -> str:
    return encode_payload(value)


def _frame(**kw: Any) -> AttributionFrame:
    base: dict[str, Any] = {"name": None, "tags": (), "metadata": {}, "user_id": None, "session_id": None}
    base.update(kw)
    return AttributionFrame(**base)


def test_nothing_outside_a_frame():
    assert merged_attributes(_encode, "r") == {}


def test_nested_frames_merge():
    with (
        pushed_frame(_frame(name="a", tags=("x", "y"), metadata={"k": 1, "j": 1}, user_id="u")),
        pushed_frame(_frame(tags=("y", "z"), metadata={"k": 2}, session_id="s")),
    ):
        merged = merged_attributes(_encode, "r")
    assert merged[attr.TRACE_NAME] == "a"
    assert orjson.loads(merged[attr.TRACE_TAGS]) == ["x", "y", "z"]
    assert orjson.loads(merged[attr.TRACE_METADATA]) == {"k": 2, "j": 1}
    assert (merged[attr.USER_ID], merged[attr.SESSION_ID]) == ("u", "s")


def test_run_version_is_the_outermost_frames():
    with (
        pushed_frame(_frame(metadata={"other": 1})),
        pushed_frame(_frame(metadata={RUN_VERSION_METADATA_KEY: 4})),
        pushed_frame(_frame(metadata={RUN_VERSION_METADATA_KEY: 7})),
    ):
        merged = merged_attributes(_encode, "r")
    assert merged[attr.RUN_VERSION] == "4"
    assert orjson.loads(merged[attr.TRACE_METADATA]) == {"other": 1}


def test_frames_end_with_their_block():
    with pushed_frame(_frame(name="a")):
        pass
    assert merged_attributes(_encode, "r") == {}
