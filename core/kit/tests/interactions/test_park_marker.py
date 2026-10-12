"""The wire-form park markers: each builder emits what its schema admits, each reader is strict.

The suspended-interaction marker carries the owner and both id lists; the resume-buffered marker
carries the still-open ids.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from tai42_contract.interactions import (
    RESUME_BUFFERED_MARKER_KEY,
    RESUME_BUFFERED_MARKER_SCHEMA,
    SUSPENDED_INTERACTION_MARKER_KEY,
    SUSPENDED_INTERACTION_MARKER_SCHEMA,
    MalformedParkMarkerError,
    NestedParkOwnershipError,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)

from tai42_kit.interactions.park_adoption import assert_park_adoptable
from tai42_kit.interactions.park_marker import (
    read_resume_buffered_marker,
    read_suspended_interaction_marker,
    resume_buffered_marker,
    suspended_interaction_marker,
)


def test_the_wire_marker_carries_the_park_owner():
    # The marker is the WIRE form a claimer reads off a serialized tool result, so the owner
    # has to ride it — the sentinel object never reaches that seam — beside both id lists.
    payload = suspended_interaction_marker(
        "i1", None, "agent_resume", interaction_ids=["i1"], caller_interaction_ids=[]
    )[SUSPENDED_INTERACTION_MARKER_KEY]
    assert payload == {
        "interaction_id": "i1",
        "expiry_at": None,
        "resume_owner": "agent_resume",
        "interaction_ids": ["i1"],
        "caller_interaction_ids": [],
    }
    # And it survives the JSON round trip the tool-output serialization puts it through.
    assert read_suspended_interaction_marker(json.dumps({SUSPENDED_INTERACTION_MARKER_KEY: payload})) == payload


def test_the_marker_builder_requires_both_id_lists():
    with pytest.raises(TypeError, match="interaction_ids"):
        suspended_interaction_marker("i1", None, "agent_resume")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="caller_interaction_ids"):
        suspended_interaction_marker("i1", None, "agent_resume", interaction_ids=["i1"])  # type: ignore[call-arg]


def _conforms(schema: dict[str, Any], instance: object) -> bool:
    """Whether ``instance`` validates against ``schema``, after checking the schema is itself valid draft 2020-12."""
    Draft202012Validator.check_schema(schema)
    validator: Any = Draft202012Validator(schema)
    return bool(validator.is_valid(instance))


@pytest.mark.parametrize(
    "marker",
    [
        suspended_interaction_marker("i1", None, None, interaction_ids=["i1"], caller_interaction_ids=[]),
        suspended_interaction_marker(
            "i1",
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            "agent_resume",
            interaction_ids=["i1", "i2"],
            caller_interaction_ids=["i2"],
        ),
    ],
)
def test_the_marker_schema_admits_every_marker_the_builder_emits(marker: dict[str, object]) -> None:
    # The schema is what an MCP edge advertises beside a pausing tool's declared output, so
    # every marker the builder emits — with or without a deadline and an owner — validates,
    # including after the JSON round trip the wire puts it through.
    assert _conforms(SUSPENDED_INTERACTION_MARKER_SCHEMA, marker)
    assert _conforms(SUSPENDED_INTERACTION_MARKER_SCHEMA, json.loads(json.dumps(marker)))


@pytest.mark.parametrize(
    "instance",
    [
        {"answer": "hi"},
        {"interaction_id": "i1"},
        {
            SUSPENDED_INTERACTION_MARKER_KEY: {
                "interaction_id": "i1",
                "expiry_at": None,
                "resume_owner": None,
                "interaction_ids": ["i1"],
            }
        },
        {
            SUSPENDED_INTERACTION_MARKER_KEY: {
                "interaction_id": "i1",
                "expiry_at": None,
                "resume_owner": None,
                "interaction_ids": ["i1"],
                "caller_interaction_ids": [],
            },
            "answer": "hi",
        },
    ],
)
def test_the_marker_schema_refuses_anything_but_the_marker(instance: dict[str, object]) -> None:
    # A result without the reserved key, a payload missing an id list, or a marker carrying a
    # second top-level field is not the park signal.
    assert not _conforms(SUSPENDED_INTERACTION_MARKER_SCHEMA, instance)


def test_the_resume_buffered_marker_round_trips_and_matches_its_schema() -> None:
    # The wire form of a ``ResumeBuffered``: the still-open sibling ids under the reserved key,
    # read back off the live dict or its JSON string, and admitted by the schema an MCP edge
    # advertises beside a pausing tool's declared output.
    marker = resume_buffered_marker(["r1", "r2"])
    assert marker == {RESUME_BUFFERED_MARKER_KEY: {"remaining_ids": ["r1", "r2"]}}
    assert read_resume_buffered_marker(marker) == {"remaining_ids": ["r1", "r2"]}
    assert read_resume_buffered_marker(json.dumps(marker)) == {"remaining_ids": ["r1", "r2"]}
    assert _conforms(RESUME_BUFFERED_MARKER_SCHEMA, marker)
    assert not _conforms(RESUME_BUFFERED_MARKER_SCHEMA, {"remaining_ids": ["r1"]})
    assert not _conforms(RESUME_BUFFERED_MARKER_SCHEMA, {RESUME_BUFFERED_MARKER_KEY: {}})
    # Neither park marker is mistaken for the other.
    park = suspended_interaction_marker("i1", None, None, interaction_ids=["i1"], caller_interaction_ids=[])
    assert read_resume_buffered_marker(park) is None
    assert read_suspended_interaction_marker(marker) is None
    assert not _conforms(RESUME_BUFFERED_MARKER_SCHEMA, park)


@pytest.mark.parametrize("content", [None, "not json", {"remaining_ids": ["r1"]}, {RESUME_BUFFERED_MARKER_KEY: "x"}])
def test_a_value_without_the_resume_buffered_marker_reads_as_none(content: object) -> None:
    assert read_resume_buffered_marker(content) is None


@pytest.mark.parametrize("payload", [{}, {"remaining_ids": "r1"}, {"remaining_ids": [1]}])
def test_a_resume_buffered_marker_without_a_valid_id_list_is_refused(payload: object) -> None:
    with pytest.raises(MalformedParkMarkerError, match="remaining_ids"):
        read_resume_buffered_marker({RESUME_BUFFERED_MARKER_KEY: payload})


def test_the_wire_marker_carries_the_step_id_lists():
    # A driver surfacing a whole super-step passes the merged per-ask lists, and both ride the
    # wire so a claimer reading a serialized tool result can tell the caller asks apart.
    payload = suspended_interaction_marker(
        "i1", None, "agent_resume", interaction_ids=["i1", "i2"], caller_interaction_ids=["i2"]
    )[SUSPENDED_INTERACTION_MARKER_KEY]
    assert payload["interaction_ids"] == ["i1", "i2"]
    assert payload["caller_interaction_ids"] == ["i2"]
    marker = read_suspended_interaction_marker(json.dumps({SUSPENDED_INTERACTION_MARKER_KEY: payload}))
    assert marker is not None
    assert marker["caller_interaction_ids"] == ["i2"]


@pytest.mark.parametrize(
    "content",
    [
        {SUSPENDED_INTERACTION_MARKER_KEY: {}},
        {SUSPENDED_INTERACTION_MARKER_KEY: "hello"},
        {SUSPENDED_INTERACTION_MARKER_KEY: {"resume_owner": "agent_resume"}},
        {SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": 5, "expiry_at": None}},
        json.dumps({SUSPENDED_INTERACTION_MARKER_KEY: {"expiry_at": None}}),
    ],
    ids=["empty-payload", "bare-string", "owner-only", "non-str-id", "json-no-id"],
)
def test_a_malformed_marker_reads_as_no_park(content: object):
    # The reserved key is present but the payload is not a well-formed marker — content a model
    # shaped, or a corrupt wire form. The reader shape-checks the payload and returns None rather
    # than a dict a caller would KeyError on and abort the run over.
    assert read_suspended_interaction_marker(content) is None


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"interaction_id": "i1", "expiry_at": None}, "interaction_ids"),
        ({"interaction_id": "i1", "interaction_ids": ["i1"]}, "caller_interaction_ids"),
        ({"interaction_id": "i1", "interaction_ids": "i1", "caller_interaction_ids": []}, "interaction_ids"),
        ({"interaction_id": "i1", "interaction_ids": ["i1"], "caller_interaction_ids": [7]}, "caller_interaction_ids"),
    ],
    ids=["no-lists", "no-caller-list", "ids-not-a-list", "caller-ids-not-strings"],
)
def test_a_marker_lacking_a_valid_id_list_is_refused(payload: dict[str, object], field: str):
    # A value naming an interaction under the reserved key but missing an id list cannot say which
    # of its asks address the caller: it is refused loudly, never read as a park of user asks only.
    content = json.dumps({SUSPENDED_INTERACTION_MARKER_KEY: payload})
    with pytest.raises(MalformedParkMarkerError, match=f"lacks a valid '{field}' list"):
        read_suspended_interaction_marker(content)


def test_a_marker_built_without_an_owner_is_not_adoptable():
    # A marker naming no owner names no driver entitled to claim it.
    marker = read_suspended_interaction_marker(
        suspended_interaction_marker("i1", None, None, interaction_ids=["i1"], caller_interaction_ids=[])
    )
    assert marker is not None
    assert marker["resume_owner"] is None
    token = set_resume_continuation_tool("agent_resume")
    try:
        with pytest.raises(NestedParkOwnershipError):
            assert_park_adoptable(marker["resume_owner"], interaction_id=marker["interaction_id"], tool_name="t")
    finally:
        reset_resume_continuation_tool(token)
