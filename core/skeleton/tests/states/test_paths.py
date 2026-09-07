"""The path-op apply engine: set/remove/append semantics, the keyed-op apply, guard
short-circuiting, and copy-on-write purity. This ONE apply backs both the persisted
store and any in-run view over the same document, so its behaviour is pinned
exhaustively here; the pure op vocabulary it applies is tested in the contract's
``test_ops`` beside :mod:`tai42_contract.states.ops`."""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import InvalidPathError

from tai42_skeleton.states.paths import apply_op, apply_ops, guard_passes, validate_op


def _set(path: list, value) -> dict:
    return {"op": "set", "path": path, "value": value}


def _remove(path: list) -> dict:
    return {"op": "remove", "path": path}


# --------------------------------------------------------------------------- #
# set                                                                          #
# --------------------------------------------------------------------------- #
def test_set_top_level():
    assert apply_ops({}, [_set(["budget"], 4200)]) == {"budget": 4200}


def test_set_replaces_existing():
    assert apply_ops({"budget": 1}, [_set(["budget"], 2)]) == {"budget": 2}


def test_set_deep_creates_intermediate_objects():
    assert apply_ops({}, [_set(["quest", "city", "name"], "Paris")]) == {"quest": {"city": {"name": "Paris"}}}


def test_set_null_is_a_stored_value():
    # At THIS layer null is a value like any other (a higher value-computation layer
    # is where null means skip; clearing is the remove op).
    assert apply_ops({}, [_set(["note"], None)]) == {"note": None}


def test_set_existing_index():
    doc = {"vetoes": [{"scope": "a"}, {"scope": "b"}]}
    out = apply_ops(doc, [_set(["vetoes", 1, "scope"], "B")])
    assert out == {"vetoes": [{"scope": "a"}, {"scope": "B"}]}


def test_set_index_past_end_is_loud():
    with pytest.raises(InvalidPathError, match="use '-' to append"):
        apply_ops({"vetoes": []}, [_set(["vetoes", 0], "x")])


def test_set_key_through_list_is_loud():
    with pytest.raises(InvalidPathError, match="requires an object"):
        apply_ops({"vetoes": []}, [_set(["vetoes", "scope"], "x")])


def test_set_index_through_object_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"quest": {}}, [_set(["quest", 0], "x")])


def test_set_through_scalar_is_loud():
    with pytest.raises(InvalidPathError, match="requires an object"):
        apply_ops({"budget": 42}, [_set(["budget", "cents"], 1)])


# --------------------------------------------------------------------------- #
# append ("-")                                                                 #
# --------------------------------------------------------------------------- #
def test_append_to_existing_list():
    out = apply_ops({"vetoes": [{"scope": "a"}]}, [_set(["vetoes", "-"], {"scope": "b"})])
    assert out == {"vetoes": [{"scope": "a"}, {"scope": "b"}]}


def test_append_creates_the_list_on_a_fresh_record():
    # The flagship case: the FIRST append must create the list itself.
    out = apply_ops({}, [_set(["vetoes", "-"], {"scope": "food"})])
    assert out == {"vetoes": [{"scope": "food"}]}


def test_append_mid_path_fills_a_new_item():
    # "-" mid-path appends a fresh item, and the rest of the path fills INSIDE it.
    out = apply_ops({}, [_set(["vetoes", "-", "scope"], "travel")])
    assert out == {"vetoes": [{"scope": "travel"}]}


def test_append_nested_list_in_new_item():
    # A "-" followed by another "-": a new item that is itself a list.
    out = apply_ops({}, [_set(["groups", "-", "-"], "first")])
    assert out == {"groups": [["first"]]}


def test_append_deep_inside_nested_structure():
    doc = {"quest": {"stops": [{"city": "Rome", "tags": []}]}}
    out = apply_ops(doc, [_set(["quest", "stops", 0, "tags", "-"], "food")])
    assert out == {"quest": {"stops": [{"city": "Rome", "tags": ["food"]}]}}


def test_append_on_object_is_loud():
    with pytest.raises(InvalidPathError, match="'-' \\(append\\) requires a list"):
        apply_ops({"quest": {}}, [_set(["quest", "-"], "x")])


# --------------------------------------------------------------------------- #
# remove                                                                       #
# --------------------------------------------------------------------------- #
def test_remove_top_level_key():
    assert apply_ops({"a": 1, "b": 2}, [_remove(["b"])]) == {"a": 1}


def test_remove_absent_key_is_a_noop():
    assert apply_ops({"a": 1}, [_remove(["ghost"])]) == {"a": 1}


def test_remove_absent_deep_path_is_a_noop():
    assert apply_ops({"a": 1}, [_remove(["ghost", "deeper", 3])]) == {"a": 1}


def test_remove_list_item_shifts_the_rest():
    assert apply_ops({"xs": ["a", "b", "c"]}, [_remove(["xs", 1])]) == {"xs": ["a", "c"]}


def test_remove_index_past_end_is_a_noop():
    assert apply_ops({"xs": ["a"]}, [_remove(["xs", 5])]) == {"xs": ["a"]}


def test_remove_nested_key():
    doc = {"quest": {"city": {"name": "Paris", "zip": "75"}}}
    assert apply_ops(doc, [_remove(["quest", "city", "zip"])]) == {"quest": {"city": {"name": "Paris"}}}


def test_remove_through_wrong_container_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"quest": {}}, [_remove(["quest", 0])])


def test_remove_with_append_token_is_refused():
    # "-" addresses no existing item; a remove path carrying it anywhere is malformed.
    with pytest.raises(InvalidPathError, match="'-'"):
        apply_ops({"xs": []}, [_remove(["xs", "-"])])
    with pytest.raises(InvalidPathError, match="'-'"):
        apply_ops({"xs": []}, [_remove(["xs", "-", "k"])])


def test_remove_with_a_value_key_is_refused():
    # A remove takes no value; a stray one is a malformed op (probably a mis-built
    # set), refused loudly like any other unknown-shape op — never silently ignored.
    with pytest.raises(InvalidPathError, match="value"):
        apply_ops({"xs": "a"}, [{"op": "remove", "path": ["xs"], "value": "stray"}])


# --------------------------------------------------------------------------- #
# purity (copy-on-write)                                                       #
# --------------------------------------------------------------------------- #
def test_input_document_is_never_mutated():
    doc = {"vetoes": [{"scope": "a"}], "quest": {"city": "Rome"}}
    snapshot = {"vetoes": [{"scope": "a"}], "quest": {"city": "Rome"}}
    apply_ops(doc, [_set(["vetoes", "-"], {"scope": "b"})])
    apply_ops(doc, [_set(["vetoes", 0, "scope"], "Z")])
    apply_ops(doc, [_remove(["quest", "city"])])
    apply_ops(doc, [_set(["new", "deep", "-"], 1)])
    assert doc == snapshot


def test_untouched_subtrees_are_shared_not_copied():
    # Copy-on-write along the touched path ONLY: the untouched sibling subtree is
    # the SAME object, not a deep copy (the performance pin).
    doc = {"touched": {"a": 1}, "untouched": {"big": [1, 2, 3]}}
    out = apply_op(doc, _set(["touched", "a"], 2))
    assert out["untouched"] is doc["untouched"]
    assert out["touched"] is not doc["touched"]


def test_ops_apply_in_order():
    out = apply_ops({}, [_set(["xs", "-"], "a"), _set(["xs", "-"], "b"), _remove(["xs", 0])])
    assert out == {"xs": ["b"]}


def test_malformed_batch_applies_nothing():
    # Every op is validated BEFORE any is applied — op[1] is bad, op[0] must not land.
    doc = {"a": 1}
    with pytest.raises(InvalidPathError):
        apply_ops(doc, [_set(["b"], 2), {"op": "explode", "path": ["c"]}])
    assert doc == {"a": 1}


def test_ops_batch_larger_than_the_cap_changes_nothing():
    ops = [{"op": "set", "path": ["a"], "value": 1}] * 1001
    with pytest.raises(InvalidPathError, match="1000"):
        apply_ops({}, ops)


def test_guard_passes_equality_mismatch_missing_and_null_expected():
    doc = {"writer": "flowA", "n": 3}
    # EQUAL → passes.
    assert guard_passes(doc, {"path": ["writer"], "expected": "flowA"})
    # MISMATCH → fails.
    assert not guard_passes(doc, {"path": ["writer"], "expected": "flowB"})
    # MISSING path with a null expected → passes (absent reads as null).
    assert guard_passes(doc, {"path": ["unclaimed"], "expected": None})
    # MISSING path with a non-null expected → fails.
    assert not guard_passes(doc, {"path": ["unclaimed"], "expected": "flowA"})
    # A stored null also satisfies a null expectation.
    assert guard_passes({"writer": None}, {"path": ["writer"], "expected": None})


def test_apply_op_ignores_the_guard_key():
    # The pure apply never enforces guards (the STORE does, under the row lock); it
    # simply applies the op and never trips on the extra key.
    assert apply_op({}, {"op": "set", "path": ["x"], "value": 1, "guard": {"path": ["w"], "expected": "t"}}) == {"x": 1}


# --------------------------------------------------------------------------- #
# keyed list ops — set_by_key / remove_by_key                                 #
# --------------------------------------------------------------------------- #
def _set_by_key(path: list, key_field: str, value) -> dict:
    return {"op": "set_by_key", "path": path, "key_field": key_field, "value": value}


def _remove_by_key(path: list, key_field: str, key) -> dict:
    return {"op": "remove_by_key", "path": path, "key_field": key_field, "key": key}


# -- set_by_key apply --------------------------------------------------------
def test_set_by_key_replaces_first_match_leaving_duplicates_untouched():
    # Duplicates of the same key are pathological data, but the op replaces exactly
    # the FIRST match in list order and leaves the rest alone (no silent dedup).
    doc = {"items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}, {"id": "a", "v": 3}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", {"id": "a", "v": 9})])
    assert out == {"items": [{"id": "a", "v": 9}, {"id": "b", "v": 2}, {"id": "a", "v": 3}]}


def test_set_by_key_appends_on_no_match():
    doc = {"items": [{"id": "a", "v": 1}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", {"id": "b", "v": 2})])
    assert out == {"items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}


def test_set_by_key_creates_absent_list_then_appends():
    assert apply_ops({}, [_set_by_key(["items"], "id", {"id": "a"})]) == {"items": [{"id": "a"}]}


def test_set_by_key_creates_absent_intermediate_objects():
    out = apply_ops({}, [_set_by_key(["a", "b", "items"], "id", {"id": "x"})])
    assert out == {"a": {"b": {"items": [{"id": "x"}]}}}


def test_set_by_key_skips_non_object_and_missing_field_items():
    # A scalar item and an object missing key_field are never matched (never an
    # error) — the new item appends past them.
    doc = {"items": ["scalar", {"other": 1}, {"id": "a", "v": 1}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", {"id": "b", "v": 2})])
    assert out == {"items": ["scalar", {"other": 1}, {"id": "a", "v": 1}, {"id": "b", "v": 2}]}


def test_set_by_key_int_key_matches():
    doc = {"items": [{"id": 1, "v": "a"}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", {"id": 1, "v": "b"})])
    assert out == {"items": [{"id": 1, "v": "b"}]}


def test_set_by_key_non_list_at_path_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"items": {"not": "a list"}}, [_set_by_key(["items"], "id", {"id": "a"})])


def test_set_by_key_json_equal_strictness_no_bool_int_cross_match():
    # A stored key of True must not be matched by an int key 1 (json_equal is strict).
    doc = {"items": [{"id": True, "v": 1}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", {"id": 1, "v": 2})])
    assert out == {"items": [{"id": True, "v": 1}, {"id": 1, "v": 2}]}


def test_set_by_key_is_pure():
    doc = {"items": [{"id": "a", "v": 1}]}
    snapshot = {"items": [{"id": "a", "v": 1}]}
    apply_ops(doc, [_set_by_key(["items"], "id", {"id": "a", "v": 99})])
    assert doc == snapshot  # input never mutated


# -- remove_by_key apply -----------------------------------------------------
def test_remove_by_key_removes_all_matches():
    doc = {"items": [{"id": "a"}, {"id": "b"}, {"id": "a"}]}
    out = apply_ops(doc, [_remove_by_key(["items"], "id", "a")])
    assert out == {"items": [{"id": "b"}]}


def test_remove_by_key_no_match_is_quiet_noop():
    doc = {"items": [{"id": "a"}]}
    assert apply_ops(doc, [_remove_by_key(["items"], "id", "z")]) == {"items": [{"id": "a"}]}


def test_remove_by_key_absent_list_is_noop():
    assert apply_ops({}, [_remove_by_key(["items"], "id", "a")]) == {}


def test_remove_by_key_through_absent_intermediate_is_noop():
    assert apply_ops({}, [_remove_by_key(["a", "items"], "id", "x")]) == {}


def test_remove_by_key_skips_non_object_and_missing_field_items():
    doc = {"items": ["scalar", {"other": 1}, {"id": "a"}]}
    out = apply_ops(doc, [_remove_by_key(["items"], "id", "a")])
    assert out == {"items": ["scalar", {"other": 1}]}


def test_remove_by_key_non_list_at_path_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"items": 5}, [_remove_by_key(["items"], "id", "a")])


def test_remove_by_key_json_equal_strictness_no_bool_int_cross_match():
    doc = {"items": [{"id": True}, {"id": 1}]}
    out = apply_ops(doc, [_remove_by_key(["items"], "id", 1)])
    assert out == {"items": [{"id": True}]}  # only the int 1 removed, True kept


def test_remove_by_key_is_pure():
    doc = {"items": [{"id": "a"}, {"id": "b"}]}
    snapshot = {"items": [{"id": "a"}, {"id": "b"}]}
    apply_ops(doc, [_remove_by_key(["items"], "id", "a")])
    assert doc == snapshot


def test_remove_by_key_list_removes_across_all_listed_keys():
    doc = {"items": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "a"}]}
    out = apply_ops(doc, [_remove_by_key(["items"], "id", ["a", "c"])])
    assert out == {"items": [{"id": "b"}]}  # every "a" and "c" gone, "b" survives


def test_remove_by_key_mixed_int_str_list():
    doc = {"items": [{"id": 1}, {"id": "x"}, {"id": 2}, {"id": "y"}]}
    out = apply_ops(doc, [_remove_by_key(["items"], "id", [1, "y"])])
    assert out == {"items": [{"id": "x"}, {"id": 2}]}


def test_remove_by_key_list_no_match_is_quiet_noop():
    doc = {"items": [{"id": "a"}]}
    assert apply_ops(doc, [_remove_by_key(["items"], "id", ["z", "q"])]) == {"items": [{"id": "a"}]}


def test_remove_by_key_empty_list_is_a_noop():
    # An empty key list is a well-defined no-op: a jq `[... | select(...)]` that
    # legitimately selected nothing lands cleanly.
    op = _remove_by_key(["items"], "id", [])
    assert validate_op(op) == op
    doc = {"items": [{"id": "a"}, {"id": "b"}]}
    assert apply_ops(doc, [op]) == doc  # doc unchanged
    # copy-on-write safe: the input is never mutated on the no-op path.
    snapshot = {"items": [{"id": "a"}, {"id": "b"}]}
    apply_ops(doc, [op])
    assert doc == snapshot


def test_remove_by_key_scalar_key_unchanged():
    # The scalar fast path stays byte-identical: same op dict in, same behavior.
    op = _remove_by_key(["items"], "id", "a")
    assert validate_op(op) == op
    doc = {"items": [{"id": "a"}, {"id": "b"}, {"id": "a"}]}
    assert apply_ops(doc, [op]) == {"items": [{"id": "b"}]}


# --------------------------------------------------------------------------- #
# merge_by_key — partial-field UPDATE of matched items (never inserts)         #
# --------------------------------------------------------------------------- #
def _merge_by_key(path: list, key_field: str, value: list) -> dict:
    return {"op": "merge_by_key", "path": path, "key_field": key_field, "value": value}


def test_merge_by_key_basic_partial_update():
    doc = {"items": [{"id": "a", "v": 1, "keep": True}, {"id": "b", "v": 2}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "v": 9, "new": "x"}])])
    # matched item shallow-merged (v overwritten, keep preserved, new added); "b" untouched.
    assert out == {"items": [{"id": "a", "v": 9, "keep": True, "new": "x"}, {"id": "b", "v": 2}]}


def test_merge_by_key_null_field_is_dropped_never_deletes():
    doc = {"items": [{"id": "a", "v": 1, "keep": "here"}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "v": None, "keep": None, "add": 2}])])
    # null-valued partial fields are dropped before the merge: v and keep are UNCHANGED
    # (null never deletes), only the non-null "add" lands.
    assert out == {"items": [{"id": "a", "v": 1, "keep": "here", "add": 2}]}


def test_merge_by_key_missing_key_is_skipped_never_upserts():
    doc = {"items": [{"id": "a", "v": 1}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "zzz", "v": 9}])])
    assert out == {"items": [{"id": "a", "v": 1}]}  # no match ⇒ skipped, NOT appended


def test_merge_by_key_first_match_only():
    doc = {"items": [{"id": "a", "v": 1}, {"id": "a", "v": 2}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "v": 9}])])
    # only the FIRST "a" is merged; the duplicate stored item is left untouched.
    assert out == {"items": [{"id": "a", "v": 9}, {"id": "a", "v": 2}]}


def test_merge_by_key_nested_object_replaced_wholesale_shallow_pinned():
    doc = {"items": [{"id": "a", "prefs": {"x": 1, "y": 2}}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "prefs": {"y": 3}}])])
    # TOP-LEVEL merge only: the nested object REPLACES wholesale, it does NOT deep-merge.
    assert out == {"items": [{"id": "a", "prefs": {"y": 3}}]}


def test_merge_by_key_multiple_partials_payload_order():
    doc = {"items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}, {"id": "c", "v": 3}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "v": 10}, {"id": "c", "v": 30}])])
    assert out == {"items": [{"id": "a", "v": 10}, {"id": "b", "v": 2}, {"id": "c", "v": 30}]}


def test_merge_by_key_int_key_matches():
    doc = {"items": [{"id": 1, "v": "a"}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": 1, "v": "b"}])])
    assert out == {"items": [{"id": 1, "v": "b"}]}


def test_merge_by_key_skips_non_object_and_missing_field_items():
    doc = {"items": ["scalar", {"other": 1}, {"id": "a", "v": 1}]}
    out = apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "v": 9}])])
    assert out == {"items": ["scalar", {"other": 1}, {"id": "a", "v": 9}]}


def test_merge_by_key_absent_list_is_noop():
    assert apply_ops({}, [_merge_by_key(["items"], "id", [{"id": "a", "v": 1}])]) == {}


def test_merge_by_key_through_absent_intermediate_is_noop():
    # merge never creates intermediates (it is an update, not an upsert).
    assert apply_ops({}, [_merge_by_key(["a", "items"], "id", [{"id": "x", "v": 1}])]) == {}


def test_merge_by_key_empty_list_is_a_noop():
    doc = {"items": [{"id": "a", "v": 1}]}
    op = _merge_by_key(["items"], "id", [])
    assert validate_op(op) == op
    assert apply_ops(doc, [op]) == doc


def test_merge_by_key_non_list_at_path_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"items": {"not": "a list"}}, [_merge_by_key(["items"], "id", [{"id": "a"}])])


def test_merge_by_key_is_pure():
    doc = {"items": [{"id": "a", "v": 1, "n": {"x": 1}}]}
    snapshot = {"items": [{"id": "a", "v": 1, "n": {"x": 1}}]}
    apply_ops(doc, [_merge_by_key(["items"], "id", [{"id": "a", "v": 99, "n": {"y": 2}}])])
    assert doc == snapshot


# --------------------------------------------------------------------------- #
# set_by_key — LIST form (many upserts, one op); single form unchanged         #
# --------------------------------------------------------------------------- #
def test_set_by_key_list_form_replace_and_append_mixed():
    doc = {"items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", [{"id": "a", "v": 9}, {"id": "c", "v": 3}])])
    # "a" REPLACED (whole item, not merged), "c" APPENDED, "b" untouched.
    assert out == {"items": [{"id": "a", "v": 9}, {"id": "b", "v": 2}, {"id": "c", "v": 3}]}


def test_set_by_key_list_form_replaces_whole_item_not_merge():
    doc = {"items": [{"id": "a", "v": 1, "extra": "gone"}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", [{"id": "a", "v": 9}])])
    assert out == {"items": [{"id": "a", "v": 9}]}  # "extra" dropped — a PUT, not a merge


def test_set_by_key_list_form_creates_absent_list_then_appends():
    out = apply_ops({}, [_set_by_key(["items"], "id", [{"id": "a"}, {"id": "b"}])])
    assert out == {"items": [{"id": "a"}, {"id": "b"}]}


def test_set_by_key_list_form_first_match_only():
    doc = {"items": [{"id": "a", "v": 1}, {"id": "a", "v": 2}]}
    out = apply_ops(doc, [_set_by_key(["items"], "id", [{"id": "a", "v": 9}])])
    assert out == {"items": [{"id": "a", "v": 9}, {"id": "a", "v": 2}]}


def test_set_by_key_single_object_form_unchanged():
    # The single-object form replaces a matching item or appends a new one.
    doc = {"items": [{"id": "a", "v": 1}]}
    assert apply_ops(doc, [_set_by_key(["items"], "id", {"id": "a", "v": 9})]) == {"items": [{"id": "a", "v": 9}]}
    assert apply_ops(doc, [_set_by_key(["items"], "id", {"id": "b", "v": 2})]) == {
        "items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    }


def test_set_by_key_list_form_empty_is_a_noop():
    doc = {"items": [{"id": "a"}]}
    op = _set_by_key(["items"], "id", [])
    assert validate_op(op) == op
    assert apply_ops(doc, [op]) == doc
    # An empty list on an ABSENT path creates nothing (no empty [] left behind).
    assert apply_ops({}, [_set_by_key(["items"], "id", [])]) == {}


# --------------------------------------------------------------------------- #
# set_by_key_each — the keyed FAN-OUT of the upsert ({name: [items…]})         #
# --------------------------------------------------------------------------- #
def _set_by_key_each(path: list, key_field: str, value) -> dict:
    return {"op": "set_by_key_each", "path": path, "key_field": key_field, "value": value}


def test_set_by_key_each_replaces_and_appends_within_one_list():
    doc = {"env": {"alpha": [{"id": "a1", "v": 1}, {"id": "a2", "v": 2}]}}
    out = apply_ops(doc, [_set_by_key_each(["env"], "id", {"alpha": [{"id": "a1", "v": 9}, {"id": "a3", "v": 3}]})])
    # "a1" REPLACED in place (whole item), "a3" APPENDED, "a2" untouched.
    assert out == {"env": {"alpha": [{"id": "a1", "v": 9}, {"id": "a2", "v": 2}, {"id": "a3", "v": 3}]}}


def test_set_by_key_each_multi_key_fan_out_in_one_op():
    # One op touches three fan-out keys: alpha upserts into an existing list, beta's
    # list is created, gamma's existing foreign list is untouched.
    doc = {"env": {"alpha": [{"id": "a1", "v": 1}], "gamma": [{"id": "g1"}]}}
    op = _set_by_key_each(["env"], "id", {"alpha": [{"id": "a1", "v": 2}], "beta": [{"id": "b1"}]})
    out = apply_ops(doc, [op])
    assert out == {"env": {"alpha": [{"id": "a1", "v": 2}], "beta": [{"id": "b1"}], "gamma": [{"id": "g1"}]}}


def test_set_by_key_each_creates_absent_container_and_intermediates():
    out = apply_ops({}, [_set_by_key_each(["outer", "env"], "id", {"alpha": [{"id": "a1"}]})])
    assert out == {"outer": {"env": {"alpha": [{"id": "a1"}]}}}


def test_set_by_key_each_empty_object_is_a_noop():
    # {} is the fan-out analog of the empty list payload: a jq that classified
    # nothing this run lands as a traced no-op — no container is created.
    assert apply_ops({}, [_set_by_key_each(["env"], "id", {})]) == {}


def test_set_by_key_each_all_empty_lists_is_a_noop():
    # Every fan-out key mapping to [] is the same degenerate payload as {} — nothing
    # is created, not even the container or the empty lists.
    assert apply_ops({}, [_set_by_key_each(["env"], "id", {"alpha": [], "beta": []})]) == {}


def test_set_by_key_each_empty_list_key_creates_nothing_beside_nonempty():
    # A mixed payload: the empty key contributes nothing (no [] left behind) while
    # the non-empty key fans out normally.
    out = apply_ops({}, [_set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}], "beta": []})])
    assert out == {"env": {"alpha": [{"id": "a1"}]}}


def test_set_by_key_each_non_object_at_path_is_loud():
    with pytest.raises(InvalidPathError, match="requires an object"):
        apply_ops({"env": [{"id": "a1"}]}, [_set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}]})])


def test_set_by_key_each_non_list_at_fanout_key_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"env": {"alpha": {"not": "a list"}}}, [_set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}]})])


def test_set_by_key_each_skips_foreign_items_and_is_json_strict():
    # Non-object items and objects missing key_field never match (never an error);
    # a stored bool key is not matched by an int key (json_equal strictness).
    doc = {"env": {"alpha": ["scalar", {"other": 1}, {"id": True}]}}
    out = apply_ops(doc, [_set_by_key_each(["env"], "id", {"alpha": [{"id": 1}]})])
    assert out == {"env": {"alpha": ["scalar", {"other": 1}, {"id": True}, {"id": 1}]}}


def test_set_by_key_each_is_pure():
    doc = {"env": {"alpha": [{"id": "a1", "v": 1}]}}
    snapshot = {"env": {"alpha": [{"id": "a1", "v": 1}]}}
    apply_ops(doc, [_set_by_key_each(["env"], "id", {"alpha": [{"id": "a1", "v": 9}]})])
    assert doc == snapshot  # input never mutated


def test_set_by_key_each_reapply_is_idempotent():
    # The keyed family's headline restart guarantee: re-applying the SAME op to the
    # committed result changes nothing (replace-on-match converges).
    doc = {"env": {"alpha": [{"id": "a0"}]}}
    op = _set_by_key_each(["env"], "id", {"alpha": [{"id": "a1", "v": 1}], "beta": [{"id": "b1"}]})
    once = apply_ops(doc, [op])
    assert apply_ops(once, [op]) == once


def test_append_fills_duplicate_on_reapply_where_set_by_key_each_does_not():
    # The N-fills-per-kind pattern this op retires: per-kind APPEND fills re-applied
    # (a restart without the ledger, a rerun_expr reusing the normal op) duplicate
    # every item — the fan-out upsert converges instead.
    appends = [_set(["env", "alpha", "-"], {"id": "a1"}), _set(["env", "beta", "-"], {"id": "b1"})]
    once = apply_ops({}, appends)
    assert apply_ops(once, appends) != once  # duplicated items
    each = _set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}], "beta": [{"id": "b1"}]})
    once_keyed = apply_ops({}, [each])
    assert once_keyed == {"env": {"alpha": [{"id": "a1"}], "beta": [{"id": "b1"}]}}
    assert apply_ops(once_keyed, [each]) == once_keyed


def test_set_by_key_each_same_item_key_across_fanout_keys_is_fine():
    # Fan-out keys address INDEPENDENT lists — the same item key under two fan-out
    # keys is two different items, never a duplicate.
    op = _set_by_key_each(["env"], "id", {"alpha": [{"id": "x1"}], "beta": [{"id": "x1"}]})
    assert validate_op(op) == op
    out = apply_ops({}, [op])
    assert out == {"env": {"alpha": [{"id": "x1"}], "beta": [{"id": "x1"}]}}


# --------------------------------------------------------------------------- #
# unset_by_key — keyed FIELD REMOVAL ({key_field, fields} entries)             #
# --------------------------------------------------------------------------- #
def _unset_by_key(path: list, key_field: str, value) -> dict:
    return {"op": "unset_by_key", "path": path, "key_field": key_field, "value": value}


def test_unset_by_key_removes_named_fields_from_matched_item():
    doc = {"items": [{"id": "a", "note": "draft", "score": 7}, {"id": "b", "note": "kept"}]}
    out = apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": "a", "fields": ["note", "score"]}])])
    # Only item "a" is touched; its identity field survives; "b" is untouched.
    assert out == {"items": [{"id": "a"}, {"id": "b", "note": "kept"}]}


def test_unset_by_key_multi_entry_clears_across_items_in_one_op():
    doc = {"items": [{"id": "a", "note": "x", "v": 1}, {"id": "b", "note": "y"}, {"id": "c"}]}
    op = _unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}, {"id": "b", "fields": ["note"]}])
    out = apply_ops(doc, [op])
    assert out == {"items": [{"id": "a", "v": 1}, {"id": "b"}, {"id": "c"}]}


def test_unset_by_key_touches_only_first_match():
    # Duplicate stored keys: only the FIRST match is touched per supplied key — the
    # merge_by_key posture, verbatim.
    doc = {"items": [{"id": "a", "note": "first"}, {"id": "a", "note": "second"}]}
    out = apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}])])
    assert out == {"items": [{"id": "a"}, {"id": "a", "note": "second"}]}


def test_unset_by_key_absent_field_is_idempotent_noop():
    # A field already absent on the matched item is a per-field no-op — the plain
    # remove's idempotent-erase posture, per field.
    doc = {"items": [{"id": "a", "v": 1}]}
    out = apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}])])
    assert out == {"items": [{"id": "a", "v": 1}]}


def test_unset_by_key_unmatched_entry_is_skipped():
    # No match ⇒ the entry is SKIPPED (never an insert, never an error) — the
    # merge_by_key no-match posture.
    doc = {"items": [{"id": "a", "v": 1}]}
    out = apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": "zz", "fields": ["v"]}])])
    assert out == {"items": [{"id": "a", "v": 1}]}


def test_unset_by_key_absent_list_is_noop():
    # An update that never inserts no-ops through an absent list — nothing created.
    assert apply_ops({}, [_unset_by_key(["items"], "id", [{"id": "a", "fields": ["v"]}])]) == {}


def test_unset_by_key_absent_intermediate_is_noop():
    assert apply_ops({}, [_unset_by_key(["outer", "items"], "id", [{"id": "a", "fields": ["v"]}])]) == {}


def test_unset_by_key_empty_value_list_is_noop():
    # [] is the family's empty-payload rule: a selection that cleared nothing this
    # run lands as a traced no-op — no intermediates created, never an error.
    assert apply_ops({}, [_unset_by_key(["items"], "id", [])]) == {}


def test_unset_by_key_empty_fields_entry_touches_nothing():
    # An entry may name ZERO fields (the merge partial carrying only its key_field):
    # its item is matched but unchanged.
    doc = {"items": [{"id": "a", "v": 1}]}
    out = apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": "a", "fields": []}])])
    assert out == {"items": [{"id": "a", "v": 1}]}


def test_unset_by_key_non_list_at_path_is_loud():
    with pytest.raises(InvalidPathError, match="requires a list"):
        apply_ops({"items": {"id": "a"}}, [_unset_by_key(["items"], "id", [{"id": "a", "fields": ["v"]}])])


def test_unset_by_key_skips_foreign_items_and_is_json_strict():
    # Non-object items and objects missing key_field never match (never an error);
    # a stored bool key is not matched by an int key (json_equal strictness).
    doc = {"items": ["scalar", {"other": 1}, {"id": True, "note": "x"}, {"id": 1, "note": "y"}]}
    out = apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": 1, "fields": ["note"]}])])
    assert out == {"items": ["scalar", {"other": 1}, {"id": True, "note": "x"}, {"id": 1}]}


def test_unset_by_key_is_pure():
    doc = {"items": [{"id": "a", "note": "x"}]}
    snapshot = {"items": [{"id": "a", "note": "x"}]}
    apply_ops(doc, [_unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}])])
    assert doc == snapshot  # input never mutated


def test_unset_by_key_reapply_is_idempotent():
    # The keyed family's restart guarantee: re-applying the SAME op to the committed
    # result changes nothing (a removed field stays removed).
    doc = {"items": [{"id": "a", "note": "x", "v": 1}, {"id": "b"}]}
    op = _unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}])
    once = apply_ops(doc, [op])
    assert once == {"items": [{"id": "a", "v": 1}, {"id": "b"}]}
    assert apply_ops(once, [op]) == once


def test_unset_by_key_batch_is_atomic_on_late_entry_type_mismatch():
    # apply_ops validates EVERY op before applying ANY: a malformed LATE entry (a
    # float key) refuses the whole batch — the earlier valid set never lands.
    doc = {"items": [{"id": "a", "note": "x"}]}
    bad = _unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}, {"id": 1.5, "fields": ["v"]}])
    with pytest.raises(InvalidPathError, match="string or an integer"):
        apply_ops(doc, [_set(["flag"], True), bad])
    assert doc == {"items": [{"id": "a", "note": "x"}]}  # nothing applied
