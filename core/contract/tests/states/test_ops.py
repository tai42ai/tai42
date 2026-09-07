"""The shared op vocabulary: path/op/guard validation, the pure query helpers
(``value_at_path``, ``json_equal``) and the keyed-op match-count observability. This
ONE grammar backs both the persisted store and any in-run view, so its refusals and
readings are pinned exhaustively here — the mutating apply is exercised beside the
engine in the skeleton."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_contract.states.errors import InvalidPathError
from tai42_contract.states.ops import (
    MAX_REMOVE_KEYS,
    json_equal,
    keyed_op_match_counts,
    validate_guard,
    validate_op,
    validate_path,
    value_at_path,
)


def _set(path: list[Any], value: Any) -> dict[str, Any]:
    return {"op": "set", "path": path, "value": value}


def _remove(path: list[Any]) -> dict[str, Any]:
    return {"op": "remove", "path": path}


# --------------------------------------------------------------------------- #
# keyed list ops — set_by_key / remove_by_key                                 #
# --------------------------------------------------------------------------- #
def _set_by_key(path: list[Any], key_field: str, value: Any) -> dict[str, Any]:
    return {"op": "set_by_key", "path": path, "key_field": key_field, "value": value}


def _remove_by_key(path: list[Any], key_field: str, key: Any) -> dict[str, Any]:
    return {"op": "remove_by_key", "path": path, "key_field": key_field, "key": key}


# --------------------------------------------------------------------------- #
# merge_by_key — partial-field UPDATE of matched items (never inserts)         #
# --------------------------------------------------------------------------- #
def _merge_by_key(path: list[Any], key_field: str, value: list[Any]) -> dict[str, Any]:
    return {"op": "merge_by_key", "path": path, "key_field": key_field, "value": value}


# --------------------------------------------------------------------------- #
# set_by_key_each — the keyed FAN-OUT of the upsert ({name: [items…]})         #
# --------------------------------------------------------------------------- #
def _set_by_key_each(path: list[Any], key_field: str, value: Any) -> dict[str, Any]:
    return {"op": "set_by_key_each", "path": path, "key_field": key_field, "value": value}


# --------------------------------------------------------------------------- #
# unset_by_key — keyed FIELD REMOVAL ({key_field, fields} entries)             #
# --------------------------------------------------------------------------- #
def _unset_by_key(path: list[Any], key_field: str, value: Any) -> dict[str, Any]:
    return {"op": "unset_by_key", "path": path, "key_field": key_field, "value": value}


# --------------------------------------------------------------------------- #
# validate_path / validate_op                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [None, "not-a-list", [], 42])
def test_path_must_be_a_nonempty_list(bad: object):
    with pytest.raises(InvalidPathError):
        validate_path(bad)


@pytest.mark.parametrize("seg", [True, False, None, 1.5, ["nested"], -1, ""])
def test_bad_segments_are_refused(seg: object):
    # bool is an int subclass — refused explicitly so True never addresses index 1.
    with pytest.raises(InvalidPathError):
        validate_path([seg])


def test_valid_segments_pass():
    assert validate_path(["a", 0, "-", "b"]) == ["a", 0, "-", "b"]


def test_set_without_value_is_refused():
    with pytest.raises(InvalidPathError, match="requires a 'value'"):
        validate_op({"op": "set", "path": ["a"]})


def test_unknown_op_is_refused():
    with pytest.raises(InvalidPathError, match="unknown op"):
        validate_op({"op": "merge", "path": ["a"], "value": 1})


def test_op_with_unknown_keys_is_refused():
    with pytest.raises(InvalidPathError, match="unknown keys"):
        validate_op({"op": "set", "path": ["a"], "value": 1, "extra": True})


def test_non_dict_op_is_refused():
    with pytest.raises(InvalidPathError, match="must be a JSON object"):
        validate_op(["set", "a"])


# --------------------------------------------------------------------------- #
# resource caps (cold-review L4)                                               #
# --------------------------------------------------------------------------- #
def test_path_longer_than_the_segment_cap_is_refused():
    # 128 segments is far beyond any real document; past it the only effect is
    # pathological nesting (json serialization recursion) on an authed surface.
    validate_path(["a"] * 128)  # at the cap: fine
    with pytest.raises(InvalidPathError, match="128"):
        validate_path(["a"] * 129)


# --------------------------------------------------------------------------- #
# pure read helpers + guard validation — value_at_path / json_equal            #
# --------------------------------------------------------------------------- #
def test_value_at_path_reads_nested_and_missing_as_null():
    doc = {"a": {"b": [10, 20]}}
    assert value_at_path(doc, ["a", "b", 1]) == 20
    assert value_at_path(doc, ["a", "b"]) == [10, 20]
    # A missing key, an index past the end, and a wrong container type ALL read as null.
    assert value_at_path(doc, ["a", "missing"]) is None
    assert value_at_path(doc, ["a", "b", 5]) is None
    assert value_at_path(doc, ["a", "b", "key"]) is None  # index-into-list via a str key
    assert value_at_path({}, ["anything"]) is None


def test_json_equal_is_strict_on_bool_vs_int():
    assert json_equal(1, 1)
    assert json_equal(1, 1.0)  # JSON has one number type
    assert not json_equal(True, 1)  # a bool is NEVER an int
    assert not json_equal(1, True)
    assert json_equal(True, True)
    assert json_equal(None, None)
    assert json_equal({"a": [1, {"b": "x"}]}, {"a": [1, {"b": "x"}]})
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})
    assert not json_equal([1, 2], [2, 1])  # arrays compare order


# --------------------------------------------------------------------------- #
# compare-and-set guard — validate_guard / validate_op guard shape            #
# --------------------------------------------------------------------------- #
def test_validate_guard_accepts_a_well_formed_guard_including_null_expected():
    assert validate_guard({"path": ["writer"], "expected": "flowA"}) == {"path": ["writer"], "expected": "flowA"}
    assert validate_guard({"path": ["writer"], "expected": None}) == {"path": ["writer"], "expected": None}


def test_validate_guard_requires_expected_key():
    with pytest.raises(InvalidPathError, match="requires an 'expected'"):
        validate_guard({"path": ["writer"]})


def test_validate_guard_refuses_empty_path_and_append_token():
    with pytest.raises(InvalidPathError, match="non-empty list"):
        validate_guard({"path": [], "expected": 1})
    with pytest.raises(InvalidPathError, match="append"):
        validate_guard({"path": ["log", "-"], "expected": 1})


def test_validate_guard_refuses_unknown_keys():
    with pytest.raises(InvalidPathError, match="unknown keys"):
        validate_guard({"path": ["writer"], "expected": 1, "extra": True})


def test_validate_op_accepts_a_guarded_op_and_validates_its_shape():
    op = {"op": "set", "path": ["x"], "value": 1, "guard": {"path": ["writer"], "expected": "flowA"}}
    assert validate_op(op) == op
    # A malformed guard on an otherwise-valid op is refused loudly.
    with pytest.raises(InvalidPathError, match="requires an 'expected'"):
        validate_op({"op": "set", "path": ["x"], "value": 1, "guard": {"path": ["writer"]}})


# -- keyed validate_op refusals (C2 / C3 / key-type / "-") -------------------
def test_validate_op_accepts_well_formed_keyed_ops():
    s = _set_by_key(["items"], "id", {"id": "a", "v": 1})
    r = _remove_by_key(["items"], "id", "a")
    assert validate_op(s) == s
    assert validate_op(r) == r


def test_set_by_key_value_must_be_object():
    with pytest.raises(InvalidPathError, match="must be a JSON object"):
        validate_op(_set_by_key(["items"], "id", [1, 2]))


def test_set_by_key_value_must_carry_key_field():
    with pytest.raises(InvalidPathError, match="must carry its key_field"):
        validate_op(_set_by_key(["items"], "id", {"v": 1}))


def test_keyed_key_must_be_str_or_int_not_float():
    with pytest.raises(InvalidPathError, match="string or an integer"):
        validate_op(_set_by_key(["items"], "id", {"id": 1.5}))
    with pytest.raises(InvalidPathError, match="string or an integer"):
        validate_op(_remove_by_key(["items"], "id", 1.5))


def test_keyed_key_must_not_be_bool_null_or_container():
    # A bare list is the remove-any-of form (valid); a scalar container is not.
    for bad in (True, None, {"x": 1}):
        with pytest.raises(InvalidPathError, match="string or an integer"):
            validate_op(_remove_by_key(["items"], "id", bad))
    # set_by_key's own identity key is never a list — it names ONE item.
    for bad in (True, None, [1], {"x": 1}):
        with pytest.raises(InvalidPathError, match="string or an integer"):
            validate_op(_set_by_key(["items"], "id", {"id": bad}))


# -- remove_by_key LIST key (the $pull + $in remove-any-of idiom) -------------
def test_remove_by_key_accepts_non_empty_scalar_list():
    op = _remove_by_key(["items"], "id", ["a", "b"])
    assert validate_op(op) == op
    op_ints = _remove_by_key(["items"], "id", [1, 2, 3])
    assert validate_op(op_ints) == op_ints


def test_remove_by_key_nested_container_in_list_is_refused():
    with pytest.raises(InvalidPathError, match="string or an integer"):
        validate_op(_remove_by_key(["items"], "id", ["a", [1, 2]]))
    with pytest.raises(InvalidPathError, match="string or an integer"):
        validate_op(_remove_by_key(["items"], "id", ["a", {"k": 1}]))


def test_remove_by_key_duplicate_in_list_is_refused():
    with pytest.raises(InvalidPathError, match="duplicate key"):
        validate_op(_remove_by_key(["items"], "id", ["a", "a"]))
    with pytest.raises(InvalidPathError, match="duplicate key"):
        validate_op(_remove_by_key(["items"], "id", [1, 2, 1]))


def test_remove_by_key_list_bool_element_is_refused():
    # bool is an int subclass — refused element-wise just like the scalar key.
    with pytest.raises(InvalidPathError, match="string or an integer"):
        validate_op(_remove_by_key(["items"], "id", ["a", True]))


def test_keyed_op_requires_non_empty_key_field():
    with pytest.raises(InvalidPathError, match="non-empty string 'key_field'"):
        validate_op({"op": "set_by_key", "path": ["items"], "key_field": "", "value": {"id": "a"}})
    with pytest.raises(InvalidPathError, match="non-empty string 'key_field'"):
        validate_op({"op": "remove_by_key", "path": ["items"], "key": "a"})


def test_set_by_key_refuses_stray_key():
    with pytest.raises(InvalidPathError, match="takes no 'key'"):
        validate_op({"op": "set_by_key", "path": ["items"], "key_field": "id", "value": {"id": "a"}, "key": "a"})


def test_remove_by_key_refuses_stray_value():
    with pytest.raises(InvalidPathError, match="takes no 'value'"):
        validate_op({"op": "remove_by_key", "path": ["items"], "key_field": "id", "key": "a", "value": 1})


def test_set_by_key_requires_value():
    with pytest.raises(InvalidPathError, match="requires a 'value'"):
        validate_op({"op": "set_by_key", "path": ["items"], "key_field": "id"})


def test_remove_by_key_requires_key():
    with pytest.raises(InvalidPathError, match="requires a 'key'"):
        validate_op({"op": "remove_by_key", "path": ["items"], "key_field": "id"})


def test_plain_ops_refuse_key_field_and_key():
    with pytest.raises(InvalidPathError, match="unknown keys"):
        validate_op({"op": "set", "path": ["x"], "value": 1, "key_field": "id"})
    with pytest.raises(InvalidPathError, match="unknown keys"):
        validate_op({"op": "remove", "path": ["x"], "key": "a"})


def test_keyed_op_refuses_append_token_in_path():
    with pytest.raises(InvalidPathError, match="append"):
        validate_op(_set_by_key(["items", "-"], "id", {"id": "a"}))
    with pytest.raises(InvalidPathError, match="append"):
        validate_op(_remove_by_key(["items", "-"], "id", "a"))


def test_remove_by_key_list_over_cap_is_refused() -> None:
    """The jq-computed key list is bounded like every other unbounded input here."""
    too_many: list[Any] = [f"k{i}" for i in range(MAX_REMOVE_KEYS + 1)]
    with pytest.raises(InvalidPathError, match="the cap is"):
        validate_op({"op": "remove_by_key", "path": ["items"], "key_field": "id", "key": too_many})


def test_remove_by_key_list_at_cap_is_accepted() -> None:
    at_cap = [f"k{i}" for i in range(MAX_REMOVE_KEYS)]
    validate_op({"op": "remove_by_key", "path": ["items"], "key_field": "id", "key": at_cap})


# -- merge_by_key validate_op refusals --------------------------------------
def test_merge_by_key_value_must_be_a_list():
    with pytest.raises(InvalidPathError, match="must be a JSON array"):
        validate_op({"op": "merge_by_key", "path": ["items"], "key_field": "id", "value": {"id": "a"}})


def test_merge_by_key_entry_must_be_object():
    with pytest.raises(InvalidPathError, match="must be a JSON object"):
        validate_op(_merge_by_key(["items"], "id", [1, 2]))


def test_merge_by_key_entry_must_carry_key_field():
    with pytest.raises(InvalidPathError, match="must carry its key_field"):
        validate_op(_merge_by_key(["items"], "id", [{"v": 1}]))


def test_merge_by_key_entry_key_must_be_str_or_int():
    for bad in (1.5, True, None, [1], {"x": 1}):
        with pytest.raises(InvalidPathError, match="string or an integer"):
            validate_op(_merge_by_key(["items"], "id", [{"id": bad}]))


def test_merge_by_key_duplicate_key_in_payload_is_refused():
    with pytest.raises(InvalidPathError, match="duplicate key"):
        validate_op(_merge_by_key(["items"], "id", [{"id": "a", "v": 1}, {"id": "a", "v": 2}]))


def test_merge_by_key_refuses_stray_key():
    with pytest.raises(InvalidPathError, match="takes no 'key'"):
        validate_op({"op": "merge_by_key", "path": ["items"], "key_field": "id", "value": [{"id": "a"}], "key": "a"})


def test_merge_by_key_requires_value():
    with pytest.raises(InvalidPathError, match="requires a 'value'"):
        validate_op({"op": "merge_by_key", "path": ["items"], "key_field": "id"})


def test_merge_by_key_requires_non_empty_key_field():
    with pytest.raises(InvalidPathError, match="non-empty string 'key_field'"):
        validate_op({"op": "merge_by_key", "path": ["items"], "key_field": "", "value": [{"id": "a"}]})


def test_merge_by_key_refuses_append_token_in_path():
    with pytest.raises(InvalidPathError, match="append"):
        validate_op(_merge_by_key(["items", "-"], "id", [{"id": "a"}]))


def test_merge_by_key_list_over_cap_is_refused():
    too_many: list[dict[str, Any]] = [{"id": f"k{i}"} for i in range(MAX_REMOVE_KEYS + 1)]
    with pytest.raises(InvalidPathError, match="the cap is"):
        validate_op(_merge_by_key(["items"], "id", too_many))


def test_merge_by_key_accepts_well_formed_op():
    op = _merge_by_key(["items"], "id", [{"id": "a", "v": 1}])
    assert validate_op(op) == op


def test_set_by_key_list_form_duplicate_key_refused():
    with pytest.raises(InvalidPathError, match="duplicate key"):
        validate_op(_set_by_key(["items"], "id", [{"id": "a", "v": 1}, {"id": "a", "v": 2}]))


def test_set_by_key_list_form_entry_must_carry_key_field():
    with pytest.raises(InvalidPathError, match="must carry its key_field"):
        validate_op(_set_by_key(["items"], "id", [{"v": 1}]))


def test_set_by_key_list_form_entry_key_type_checked():
    with pytest.raises(InvalidPathError, match="string or an integer"):
        validate_op(_set_by_key(["items"], "id", [{"id": 1.5}]))


def test_set_by_key_scalar_value_still_refused():
    with pytest.raises(InvalidPathError, match="must be a JSON object"):
        validate_op(_set_by_key(["items"], "id", 5))


# --------------------------------------------------------------------------- #
# keyed_op_match_counts — the state-write trace observability shape            #
# --------------------------------------------------------------------------- #
def test_match_counts_none_for_plain_ops():
    assert keyed_op_match_counts({}, _set(["x"], 1)) is None
    assert keyed_op_match_counts({}, _remove(["x"])) is None


def test_match_counts_merge_reports_skipped_partials():
    # After a merge where one partial matched and one raced-away/typo'd, the result
    # shows matched < supplied — the diagnostic this exists for.
    result = {"items": [{"id": "a", "v": 9}, {"id": "b", "v": 2}]}
    op = _merge_by_key(["items"], "id", [{"id": "a", "v": 9}, {"id": "zzz", "v": 0}])
    assert keyed_op_match_counts(result, op) == {"supplied": 2, "matched": 1}


def test_match_counts_set_all_land():
    result = {"items": [{"id": "a"}, {"id": "b"}]}
    op = _set_by_key(["items"], "id", [{"id": "a"}, {"id": "b"}])
    assert keyed_op_match_counts(result, op) == {"supplied": 2, "matched": 2}


def test_match_counts_single_set_and_scalar_remove():
    result = {"items": [{"id": "a"}]}
    assert keyed_op_match_counts(result, _set_by_key(["items"], "id", {"id": "a"})) == {"supplied": 1, "matched": 1}
    # scalar remove: the key is absent from the result ⇒ matched (removed) == supplied.
    assert keyed_op_match_counts({"items": []}, _remove_by_key(["items"], "id", "a")) == {"supplied": 1, "matched": 1}


def test_match_counts_remove_list_residual():
    # "a" removed (absent from result ⇒ matched), "b" still present ⇒ NOT matched.
    result = {"items": [{"id": "b"}]}
    op = _remove_by_key(["items"], "id", ["a", "b"])
    assert keyed_op_match_counts(result, op) == {"supplied": 2, "matched": 1}


def test_match_counts_missing_list_reads_as_empty():
    op = _merge_by_key(["items"], "id", [{"id": "a"}])
    assert keyed_op_match_counts({}, op) == {"supplied": 1, "matched": 0}


# -- set_by_key_each validation ----------------------------------------------
def test_set_by_key_each_accepts_well_formed_op():
    op = _set_by_key_each(["env"], "id", {"alpha": [{"id": "a1", "v": 1}]})
    assert validate_op(op) == op


def test_set_by_key_each_value_must_be_object():
    for bad in ([{"id": "a1"}], "alpha", 5, None):
        with pytest.raises(InvalidPathError, match="must be a JSON object"):
            validate_op(_set_by_key_each(["env"], "id", bad))


def test_set_by_key_each_fanout_key_must_be_valid_segment():
    # A fan-out key becomes a path segment (path + [key]): empty and "-" are refused
    # exactly as they would be in a keyed path.
    with pytest.raises(InvalidPathError, match="fan-out key"):
        validate_op(_set_by_key_each(["env"], "id", {"": [{"id": "a1"}]}))
    with pytest.raises(InvalidPathError, match="fan-out key"):
        validate_op(_set_by_key_each(["env"], "id", {"-": [{"id": "a1"}]}))


def test_set_by_key_each_entry_list_required_per_key():
    with pytest.raises(InvalidPathError, match="array of item objects"):
        validate_op(_set_by_key_each(["env"], "id", {"alpha": {"id": "a1"}}))


def test_set_by_key_each_entry_must_carry_key_field():
    with pytest.raises(InvalidPathError, match="must carry its key_field"):
        validate_op(_set_by_key_each(["env"], "id", {"alpha": [{"v": 1}]}))


def test_set_by_key_each_duplicate_item_keys_within_one_list_refused():
    with pytest.raises(InvalidPathError, match="duplicate key"):
        validate_op(_set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}, {"id": "a1"}]}))


def test_set_by_key_each_requires_key_field_value_and_refuses_stray_key():
    with pytest.raises(InvalidPathError, match="key_field"):
        validate_op({"op": "set_by_key_each", "path": ["env"], "value": {}})
    with pytest.raises(InvalidPathError, match="requires a 'value'"):
        validate_op({"op": "set_by_key_each", "path": ["env"], "key_field": "id"})
    with pytest.raises(InvalidPathError, match="takes no 'key'"):
        validate_op(_set_by_key_each(["env"], "id", {"alpha": []}) | {"key": "a1"})


def test_set_by_key_each_refuses_append_token_in_path():
    with pytest.raises(InvalidPathError, match="has no meaning"):
        validate_op(_set_by_key_each(["env", "-"], "id", {"alpha": [{"id": "a1"}]}))


def test_set_by_key_each_fanout_keys_over_cap_refused():
    too_many: dict[str, Any] = {f"k{i}": [] for i in range(MAX_REMOVE_KEYS + 1)}
    with pytest.raises(InvalidPathError, match="the cap is"):
        validate_op(_set_by_key_each(["env"], "id", too_many))


def test_set_by_key_each_per_key_item_list_over_cap_refused():
    too_many: dict[str, Any] = {"alpha": [{"id": f"a{i}"} for i in range(MAX_REMOVE_KEYS + 1)]}
    with pytest.raises(InvalidPathError, match="the cap is"):
        validate_op(_set_by_key_each(["env"], "id", too_many))


def test_match_counts_set_by_key_each_all_land():
    # The fan-out upsert always lands every item, like set_by_key: supplied counts
    # items across ALL fan-out keys, matched reads each fanned list in the result.
    result = {"env": {"alpha": [{"id": "a1"}, {"id": "a2"}], "beta": [{"id": "b1"}]}}
    op = _set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}, {"id": "a2"}], "beta": [{"id": "b1"}]})
    assert keyed_op_match_counts(result, op) == {"supplied": 3, "matched": 3}


def test_match_counts_set_by_key_each_missing_container_reads_as_empty():
    op = _set_by_key_each(["env"], "id", {"alpha": [{"id": "a1"}]})
    assert keyed_op_match_counts({}, op) == {"supplied": 1, "matched": 0}


# -- unset_by_key validation ---------------------------------------------------
def test_unset_by_key_accepts_well_formed_op():
    op = _unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}])
    assert validate_op(op) == op


def test_unset_by_key_value_must_be_list():
    for bad in ({"id": "a", "fields": ["v"]}, "a", 5, None):
        with pytest.raises(InvalidPathError, match="must be a JSON array"):
            validate_op(_unset_by_key(["items"], "id", bad))


def test_unset_by_key_entry_must_be_object():
    with pytest.raises(InvalidPathError, match="must be a JSON object"):
        validate_op(_unset_by_key(["items"], "id", ["a"]))


def test_unset_by_key_entry_must_carry_key_field():
    with pytest.raises(InvalidPathError, match="must carry its key_field"):
        validate_op(_unset_by_key(["items"], "id", [{"fields": ["v"]}]))


def test_unset_by_key_entry_requires_fields_list():
    with pytest.raises(InvalidPathError, match="requires a 'fields' list"):
        validate_op(_unset_by_key(["items"], "id", [{"id": "a"}]))
    with pytest.raises(InvalidPathError, match="requires a 'fields' list"):
        validate_op(_unset_by_key(["items"], "id", [{"id": "a", "fields": "note"}]))


def test_unset_by_key_field_names_must_be_nonempty_strings():
    for bad_name in ("", 3, None, ["nested"]):
        with pytest.raises(InvalidPathError, match="non-empty string"):
            validate_op(_unset_by_key(["items"], "id", [{"id": "a", "fields": [bad_name]}]))


def test_unset_by_key_refuses_unsetting_the_key_field_itself():
    # An item must keep its identity — removing key_field would orphan the item for
    # every later keyed op. Refused loudly at validation (bind-time via the probe).
    with pytest.raises(InvalidPathError, match="keeps its identity"):
        validate_op(_unset_by_key(["items"], "id", [{"id": "a", "fields": ["id"]}]))


def test_unset_by_key_duplicate_field_names_refused():
    with pytest.raises(InvalidPathError, match="duplicate field"):
        validate_op(_unset_by_key(["items"], "id", [{"id": "a", "fields": ["note", "note"]}]))


def test_unset_by_key_duplicate_entry_keys_refused():
    with pytest.raises(InvalidPathError, match="duplicate key"):
        validate_op(_unset_by_key(["items"], "id", [{"id": "a", "fields": ["v"]}, {"id": "a", "fields": ["note"]}]))


def test_unset_by_key_entry_unknown_keys_refused():
    # An entry is the {key_field, fields} envelope, never a partial item — stray
    # keys (a merge payload pasted into an unset) are refused loudly.
    with pytest.raises(InvalidPathError, match="unknown keys"):
        validate_op(_unset_by_key(["items"], "id", [{"id": "a", "fields": ["v"], "note": "x"}]))


def test_unset_by_key_key_field_named_fields_refused():
    # The entry envelope reserves the name "fields" — an identity field so named
    # would collide with the envelope key inside every entry.
    with pytest.raises(InvalidPathError, match="reserves"):
        validate_op(_unset_by_key(["items"], "fields", [{"fields": ["v"]}]))


def test_unset_by_key_requires_key_field_value_and_refuses_stray_key():
    with pytest.raises(InvalidPathError, match="key_field"):
        validate_op({"op": "unset_by_key", "path": ["items"], "value": []})
    with pytest.raises(InvalidPathError, match="requires a 'value'"):
        validate_op({"op": "unset_by_key", "path": ["items"], "key_field": "id"})
    with pytest.raises(InvalidPathError, match="takes no 'key'"):
        validate_op(_unset_by_key(["items"], "id", []) | {"key": "a"})


def test_unset_by_key_refuses_append_token_in_path():
    with pytest.raises(InvalidPathError, match="has no meaning"):
        validate_op(_unset_by_key(["items", "-"], "id", [{"id": "a", "fields": ["v"]}]))


def test_unset_by_key_entries_over_cap_refused():
    too_many: list[dict[str, Any]] = [{"id": f"k{i}", "fields": []} for i in range(MAX_REMOVE_KEYS + 1)]
    with pytest.raises(InvalidPathError, match="the cap is"):
        validate_op(_unset_by_key(["items"], "id", too_many))


def test_unset_by_key_fields_over_cap_refused():
    too_many: list[dict[str, Any]] = [{"id": "a", "fields": [f"f{i}" for i in range(MAX_REMOVE_KEYS + 1)]}]
    with pytest.raises(InvalidPathError, match="the cap is"):
        validate_op(_unset_by_key(["items"], "id", too_many))


def test_match_counts_unset_by_key_counts_skipped_entries():
    # Intent mirrors merge_by_key: matched = the entry's item is PRESENT in the
    # result; supplied - matched = the silently-skipped entries.
    result = {"items": [{"id": "a"}]}
    op = _unset_by_key(["items"], "id", [{"id": "a", "fields": ["note"]}, {"id": "zz", "fields": ["v"]}])
    assert keyed_op_match_counts(result, op) == {"supplied": 2, "matched": 1}


def test_match_counts_unset_by_key_missing_list_reads_as_empty():
    op = _unset_by_key(["items"], "id", [{"id": "a", "fields": ["v"]}])
    assert keyed_op_match_counts({}, op) == {"supplied": 1, "matched": 0}
