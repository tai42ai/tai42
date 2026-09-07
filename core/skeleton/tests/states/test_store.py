"""The store's pure helpers — the trace-stamping rule (D-3), the composing-shape refusal
(D-4), the regime/traced-path derivation from mount rows, and the op-ledger retention
validation. The SQL behaviors (subject-keyed read/apply/fold/alias/search/migrate and the
``state_writes`` ledger) are exercised against a real Postgres in
``test_store_integration.py``."""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import RegimeViolationError, StatesError, ValueValidationError

from tai42_skeleton.states import store as store_mod
from tai42_skeleton.states.store import (
    _abs_regime_paths,
    _refuse_composing_shape,
    _split_cursor,
    _traced_paths,
    make_cursor,
    stamp_trace,
    store_settings_retention,
)

_STAMP = {"meta": {"node": "n1"}, "run": "r1", "turn": "t1", "inbound": "i1", "at": "2026-09-06T00:00:00+00:00"}


def _mounts() -> list[dict]:
    return [
        {
            "module": "traced",
            "path": ["a"],
            "body": {
                "regimes": [{"path": ["items"], "regime": "composing"}],
                "trace": {"enabled": True},
                "name": "traced",
            },
        },
        {"module": "plain", "path": ["b"], "body": {"name": "plain"}},
    ]


def test_abs_regime_paths_from_mounts() -> None:
    paths = _abs_regime_paths(_mounts())
    assert (["a", "items"], "composing", "traced") in paths


def test_traced_paths_only_tracing_mounts() -> None:
    assert _traced_paths(_mounts()) == (("a",),)


def test_composing_shape_refuses_whole_path_set() -> None:
    regime_paths = _abs_regime_paths(_mounts())
    with pytest.raises(RegimeViolationError, match="composing path"):
        _refuse_composing_shape([{"op": "set", "path": ["a", "items"], "value": []}], regime_paths)
    with pytest.raises(RegimeViolationError):
        _refuse_composing_shape([{"op": "remove", "path": ["a", "items"]}], regime_paths)


def test_composing_shape_allows_keyed_and_append() -> None:
    regime_paths = _abs_regime_paths(_mounts())
    # a keyed op and an append set are the legitimate composing writers — no refusal
    _refuse_composing_shape(
        [{"op": "set_by_key", "path": ["a", "items"], "key_field": "id", "value": {"id": 1}}], regime_paths
    )
    _refuse_composing_shape([{"op": "set", "path": ["a", "items", "-"], "value": {}}], regime_paths)
    # a write outside any composing path is fine
    _refuse_composing_shape([{"op": "set", "path": ["b", "x"], "value": 1}], regime_paths)


def test_stamp_trace_object_set_and_keyed_items() -> None:
    traced = (("a",),)
    ops = [
        {"op": "set", "path": ["a", "obj"], "value": {"k": 1}},
        {"op": "set_by_key", "path": ["a", "items"], "key_field": "id", "value": {"id": 1}},
        {"op": "merge_by_key", "path": ["a", "items"], "key_field": "id", "value": [{"id": 2}]},
        {"op": "set_by_key_each", "path": ["a", "fan"], "key_field": "id", "value": {"g": [{"id": 3}]}},
    ]
    stamp_trace(ops, traced, _STAMP)
    assert ops[0]["value"]["_trace"] == _STAMP  # object-valued set stamped
    assert ops[1]["value"]["_trace"] == _STAMP  # keyed item object stamped
    assert ops[2]["value"][0]["_trace"] == _STAMP  # keyed item in a list stamped
    assert ops[3]["value"]["g"][0]["_trace"] == _STAMP  # fanned item stamped


def test_stamp_trace_leaves_scalars_and_untraced_untouched() -> None:
    traced = (("a",),)
    ops = [
        {"op": "set", "path": ["a", "n"], "value": 5},  # scalar under a traced mount — untouched
        {"op": "set", "path": ["b", "obj"], "value": {"k": 1}},  # object under an UNTRACED mount — untouched
        {"op": "remove", "path": ["a", "gone"]},  # remove carries no value
    ]
    stamp_trace(ops, traced, _STAMP)
    assert ops[0]["value"] == 5
    assert "_trace" not in ops[1]["value"]
    assert "value" not in ops[2]


def test_stamp_trace_noop_without_traced_paths() -> None:
    ops = [{"op": "set", "path": ["a", "obj"], "value": {"k": 1}}]
    stamp_trace(ops, (), _STAMP)
    assert "_trace" not in ops[0]["value"]


def test_store_settings_retention_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(store_mod, "states_settings", lambda: SimpleNamespace(op_retention_days=30))
    assert store_settings_retention() == 30
    monkeypatch.setattr(store_mod, "states_settings", lambda: SimpleNamespace(op_retention_days=0))
    with pytest.raises(StatesError, match="OP_RETENTION_DAYS"):
        store_settings_retention()


def test_split_cursor_round_trips_a_packed_identity() -> None:
    packed = make_cursor("agent", "a", "person", "p-1")
    assert _split_cursor(packed) == ("agent", "a", "person", "p-1")


def test_split_cursor_refuses_a_malformed_cursor() -> None:
    # A client-supplied cursor that is not a packed four-part identity is a 422, never a
    # 500 from unpacking deep in the subjects/search query.
    with pytest.raises(ValueValidationError, match="malformed"):
        _split_cursor("not-a-packed-cursor")
