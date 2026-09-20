"""int_bounds: tighten every integer node to the platform int64 range
(``inject_int64_bounds``) and find the first out-of-range integer in a value
(``find_oversized_int``) against a caller-supplied bound.
"""

from tai42_kit.utils.data.json_schema_util import (
    INT64_MAX,
    INT64_MIN,
    MSGPACK_INT_MAX,
    MSGPACK_INT_MIN,
    find_oversized_int,
    inject_int64_bounds,
)


# --------------------------------------------------------------------------- #
# inject_int64_bounds — tighten every integer node to the platform int64 range
# --------------------------------------------------------------------------- #
def test_inject_bounds_sets_absent_integer_bounds():
    out = inject_int64_bounds({"type": "integer"})
    assert out == {"type": "integer", "minimum": INT64_MIN, "maximum": INT64_MAX}


def test_inject_bounds_tightens_never_loosens():
    # An existing bound inside int64 is kept (tighter wins); a bound wider than
    # int64 is clamped to the int64 edge.
    out = inject_int64_bounds({"type": "integer", "minimum": -5, "maximum": 10**40})
    assert out["minimum"] == -5
    assert out["maximum"] == INT64_MAX


def test_inject_bounds_does_not_mutate_input():
    original = {"type": "integer"}
    inject_int64_bounds(original)
    assert original == {"type": "integer"}


def test_inject_bounds_leaves_number_nodes_untouched():
    out = inject_int64_bounds({"type": "number"})
    assert out == {"type": "number"}


def test_inject_bounds_walks_objects_arrays_and_anyof():
    schema = {
        "type": "object",
        "properties": {
            "flat": {"type": "integer"},
            "nested": {"type": "object", "properties": {"k": {"type": "integer"}}},
            "arr": {"type": "array", "items": {"type": "integer"}},
            "tuple": {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}]},
            "choice": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            "either": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
            "map": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
        "$defs": {"Ref": {"type": "integer"}},
    }
    out = inject_int64_bounds(schema)
    props = out["properties"]
    assert props["flat"]["maximum"] == INT64_MAX
    assert props["nested"]["properties"]["k"]["maximum"] == INT64_MAX
    assert props["arr"]["items"]["minimum"] == INT64_MIN
    assert props["tuple"]["prefixItems"][0]["maximum"] == INT64_MAX
    assert props["tuple"]["prefixItems"][1] == {"type": "string"}  # non-integer untouched
    assert props["choice"]["anyOf"][0]["maximum"] == INT64_MAX
    assert props["either"]["oneOf"][0]["maximum"] == INT64_MAX
    assert props["map"]["additionalProperties"]["maximum"] == INT64_MAX
    assert out["$defs"]["Ref"]["maximum"] == INT64_MAX


def test_inject_bounds_covers_nullable_integer_type_list():
    out = inject_int64_bounds({"type": ["integer", "null"]})
    assert out["maximum"] == INT64_MAX
    assert out["minimum"] == INT64_MIN


# --------------------------------------------------------------------------- #
# find_oversized_int — shared walker with a caller-supplied bound
# --------------------------------------------------------------------------- #
def test_find_oversized_int_names_nested_path_and_value():
    over = INT64_MAX + 1
    found = find_oversized_int({"a": [0, {"b": over}]}, minimum=INT64_MIN, maximum=INT64_MAX)
    assert found == ("['a'][1]['b']", over)


def test_find_oversized_int_in_range_returns_none():
    assert find_oversized_int({"n": INT64_MAX, "m": INT64_MIN}, minimum=INT64_MIN, maximum=INT64_MAX) is None


def test_find_oversized_int_bound_is_caller_supplied():
    # The int64 platform bound flags a value in (INT64_MAX, MSGPACK_INT_MAX], but
    # the wider msgpack bound encodes it fine and reports nothing there.
    value = {"x": INT64_MAX + 1}
    assert find_oversized_int(value, minimum=INT64_MIN, maximum=INT64_MAX) == ("['x']", INT64_MAX + 1)
    assert find_oversized_int(value, minimum=MSGPACK_INT_MIN, maximum=MSGPACK_INT_MAX) is None


def test_find_oversized_int_msgpack_bound_flags_only_true_culprit():
    # A payload carrying BOTH a uint64 (encodes fine) and a > 2**64 value (aborts
    # msgpack) names the > 2**64 value as the real culprit under the msgpack bound.
    payload = {"uint": 2**64 - 1, "huge": 2**64}
    assert find_oversized_int(payload, minimum=MSGPACK_INT_MIN, maximum=MSGPACK_INT_MAX) == ("['huge']", 2**64)


def test_find_oversized_int_ignores_bool():
    # bool is an int subclass but always encodes; it is never flagged.
    assert find_oversized_int({"flag": True}, minimum=0, maximum=0) is None
