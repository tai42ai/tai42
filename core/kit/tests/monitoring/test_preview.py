"""The structurally-bounded run-list preview of a trace's input/output."""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue

from tai42_kit.monitoring import TRACE_PREVIEW_MAX_CHARS, preview


def test_trace_preview_bounds_structurally():
    # None and scalars pass through unchanged; a structured value is returned as a
    # bounded JSON value, not a JSON string.
    assert preview(None) is None
    assert preview("short") == "short"
    assert preview(42) == 42
    assert preview({"a": 1}) == {"a": 1}
    # A non-JSON leaf (datetime) falls back to str.
    assert preview(datetime(2026, 1, 1)) == "2026-01-01 00:00:00"

    # Depth: nesting past the cap is elided with a terminal marker.
    deep = {"l0": {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": "x"}}}}}}}}
    node: JsonValue = preview(deep)
    for level in ("l0", "l1", "l2", "l3", "l4", "l5"):
        assert isinstance(node, dict)
        node = node[level]
    assert node == {"…": "…"}

    # Items: an object / array past the cap keeps a "+N more" marker.
    wide_obj = preview({str(i): i for i in range(30)})
    assert isinstance(wide_obj, dict)
    assert len(wide_obj) == 21  # 20 kept + the marker key
    assert wide_obj["…"] == "+10 more"
    wide_list = preview(list(range(30)))
    assert isinstance(wide_list, list)
    assert len(wide_list) == 21
    assert wide_list[-1] == "+10 more"

    # Leaf cut: a long string leaf is clipped to the cap plus the terminal marker.
    long = "x" * (TRACE_PREVIEW_MAX_CHARS + 100)
    assert preview(long) == "x" * TRACE_PREVIEW_MAX_CHARS + "…"

    # A stringified structure is parsed and bounded as a tree.
    assert preview('{"a": 1}') == {"a": 1}
    # A stringified structure over the parse gate is NOT parsed — just char-clipped.
    big = "[" + "1," * 20_000 + "1]"
    clipped = preview(big)
    assert isinstance(clipped, str)
    assert clipped == big[:TRACE_PREVIEW_MAX_CHARS] + "…"

    # Sequence bounding slices BEFORE materializing: only the kept prefix is
    # consumed, never a full copy of the whole sequence. A list whose __iter__
    # raises proves the slice happens on the sequence, not on a full copy of it.
    class _NoFullCopy(list[int]):
        def __iter__(self) -> object:  # type: ignore[override]
            raise AssertionError("preview must slice before copying the whole sequence")

    bounded = preview(_NoFullCopy(range(25)))
    assert isinstance(bounded, list)
    assert len(bounded) == 21  # 20 kept + the marker
    assert bounded[:3] == [0, 1, 2]
    assert bounded[-1] == "+5 more"

    # A long non-JSON leaf (str fallback) is cut at the cap with the same marker
    # as a string leaf — never emitted whole.
    class _LongRepr:
        def __str__(self) -> str:
            return "y" * (TRACE_PREVIEW_MAX_CHARS + 100)

    assert preview(_LongRepr()) == "y" * TRACE_PREVIEW_MAX_CHARS + "…"


def test_a_python_repr_structure_is_bounded_as_a_tree():
    assert preview("{'a': True, 'b': None}") == {"a": True, "b": None}
    assert preview("[not, json") == "[not, json"
