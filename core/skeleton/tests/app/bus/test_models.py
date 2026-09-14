"""Worker-bus value types and pure helpers: decode, beat-age, identity, freshness,
terminal merge."""

from __future__ import annotations

import json

import pytest

from tai42_skeleton.app.bus import OpOutcome, WorkerResult, presence_fresh
from tai42_skeleton.app.bus.models import _beat_age_seconds, _decode, _merge_terminal

from .conftest import make_bus


def test_decode_rejects_non_json_and_non_object_frames() -> None:
    # A malformed wire frame is discarded (logged), never applied.
    assert _decode("this is not json") is None
    assert _decode(json.dumps(["not", "an", "object"])) is None
    assert _decode(json.dumps({"op": "x"})) == {"op": "x"}


def test_beat_age_seconds_degrades_on_a_bad_stamp() -> None:
    # A cosmetic-only helper never crashes the fleet publish: an unparseable stamp
    # AND a valid-but-naive one (no offset — the aware-minus-naive subtract would
    # raise TypeError) both degrade to None.
    assert _beat_age_seconds("not-a-timestamp") is None
    assert _beat_age_seconds("2020-01-01T00:00:00") is None


def test_identity_raises_before_the_slot_is_claimed() -> None:
    # A real bus has no identity until it claims a slot at subscribe time; reading it
    # before then raises loudly rather than emitting a placeholder name.
    bus = make_bus()
    bus._identity = None
    with pytest.raises(RuntimeError, match="not minted"):
        _ = bus.identity


def test_presence_fresh_is_the_sole_home_of_the_bound() -> None:
    ttl = 15.0  # bound = ttl - 2*(ttl/3) = ttl/3 = 5s -> 5000ms
    assert presence_fresh(6000, ttl) is True
    assert presence_fresh(5000, ttl) is False  # AT the bound is not fresh (strictly above)
    assert presence_fresh(4000, ttl) is False
    assert presence_fresh(None, ttl) is False


def test_merge_terminal_failure_is_never_overridden() -> None:
    applied = WorkerResult(name="serve-1", outcome=OpOutcome.applied)
    failed = WorkerResult(name="serve-1", outcome=OpOutcome.failed, error="boom")

    terminal: dict[str, WorkerResult] = {}
    _merge_terminal(terminal, "serve-1", applied)
    _merge_terminal(terminal, "serve-1", failed)
    assert terminal["serve-1"].outcome == OpOutcome.failed

    terminal = {}
    _merge_terminal(terminal, "serve-1", failed)
    _merge_terminal(terminal, "serve-1", applied)
    assert terminal["serve-1"].outcome == OpOutcome.failed
