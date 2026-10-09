"""The evaluation envelope reaches jq as JSON text, from orjson where it matches ``json.dumps``, else from it."""

import collections
import dataclasses
import datetime
import decimal
import enum
import json
import math
import uuid
from typing import Any, NamedTuple

import pytest

pytest.importorskip("jq")

import numpy as np

from tai42_kit.utils.data import jq_util
from tai42_kit.utils.data.jq_util import run_jq_bounded, run_jq_first


def _through_json_dumps(payload: Any, expression: str = ".") -> Any:
    """The result jq gives when the envelope is encoded by ``json.dumps``."""
    text = json.dumps(jq_util._envelope(payload, {}))
    return jq_util.get_compiled_jq(expression).input_text(text).first()


class _Str(str):
    pass


class _Int(int):
    pass


class _Float(float):
    pass


class _List(list):
    pass


class _Tuple(tuple):
    pass


class _Pair(NamedTuple):
    left: int
    right: str


class _IntKind(enum.IntEnum):
    ONE = 1


class _StrKind(enum.StrEnum):
    RED = "red"


class _Plain(enum.Enum):
    X = "x"


@dataclasses.dataclass
class _Record:
    a: int


_VALUES = {
    "ordered_dict": collections.OrderedDict([("b", 1), ("a", 2)]),
    "default_dict": collections.defaultdict(list, {"k": [1]}),
    "str_subclass": _Str("s"),
    "int_subclass": _Int(7),
    "list_subclass": _List([1, 2]),
    "int_enum": _IntKind.ONE,
    "str_enum": _StrKind.RED,
    "tuple": (1, "two", 3.0),
    "numpy_float64": np.float64(1.5),
    "float_subclass": _Float(2.25),
    "namedtuple": _Pair(1, "r"),
    "tuple_subclass": _Tuple((4, 5)),
    "big_int": 2**70,
}


class TestValueParity:
    @pytest.mark.parametrize("name", sorted(_VALUES))
    async def test_a_value_alone_gives_todays_result(self, name):
        value = _VALUES[name]
        assert await run_jq_first(".", value) == _through_json_dumps(value)

    @pytest.mark.parametrize("name", sorted(_VALUES))
    async def test_a_value_nested_gives_todays_result(self, name):
        payload = {"outer": [{"inner": _VALUES[name]}, _VALUES[name]]}
        assert await run_jq_first(".", payload) == _through_json_dumps(payload)

    async def test_a_mixed_payload_gives_todays_result(self):
        payload = {"all": list(_VALUES.values()), "map": dict(_VALUES)}
        assert await run_jq_first(".", payload) == _through_json_dumps(payload)

    async def test_run_jq_bounded_gives_todays_result(self):
        payload = [_VALUES["namedtuple"], _VALUES["numpy_float64"], _VALUES["ordered_dict"]]
        assert await run_jq_bounded(".[]", payload, limit=5) == [
            _through_json_dumps(payload, ".[0]"),
            _through_json_dumps(payload, ".[1]"),
            _through_json_dumps(payload, ".[2]"),
        ]

    @pytest.mark.parametrize(
        "value",
        [
            datetime.datetime(2026, 1, 2, 3, 4, 5),
            _Record(1),
            {1, 2},
            decimal.Decimal("1.5"),
            np.int64(3),
            np.float32(1.5),
            np.bool_(True),
        ],
        ids=["datetime", "dataclass", "set", "decimal", "numpy_int64", "numpy_float32", "numpy_bool"],
    )
    async def test_a_value_json_dumps_refuses_raises_its_text(self, value):
        with pytest.raises(TypeError) as today:
            json.dumps(jq_util._envelope({"x": value}, {}))
        with pytest.raises(TypeError) as now:
            await run_jq_first(".", {"x": value})
        assert str(now.value) == str(today.value)


class TestStatedEncodingChanges:
    async def test_a_nan_reaches_jq_as_null(self):
        assert await run_jq_first(".a", {"a": math.nan}) is None
        assert await run_jq_first(".a", {"a": math.inf}) is None

    async def test_a_plain_enum_reaches_jq_as_its_value(self):
        assert await run_jq_first(".a", {"a": _Plain.X}) == "x"

    async def test_a_uuid_reaches_jq_as_its_string(self):
        assert await run_jq_first(".a", {"a": uuid.UUID(int=1)}) == "00000000-0000-0000-0000-000000000001"


_TODAY_KEYS = {
    "str_subclass": _Str("k"),
    "str_enum": _StrKind.RED,
    "int": 3,
    "int_enum": _IntKind.ONE,
    "bool": True,
    "none": None,
    "float": 1.5,
    "small_float": 1e-07,
    "nan": math.nan,
    "big_int": 2**70,
    "float_subclass": _Float(1.5),
    "numpy_float64": np.float64(1.5),
}


class TestKeyParity:
    @pytest.mark.parametrize("name", sorted(_TODAY_KEYS))
    async def test_a_key_alone_gives_todays_result(self, name):
        payload = {_TODAY_KEYS[name]: "v"}
        assert await run_jq_first(".", payload) == _through_json_dumps(payload)

    @pytest.mark.parametrize("name", sorted(_TODAY_KEYS))
    async def test_a_key_nested_gives_todays_result(self, name):
        payload = {"outer": [{"deep": {_TODAY_KEYS[name]: "v"}}]}
        assert await run_jq_first(".", payload) == _through_json_dumps(payload)

    async def test_the_key_texts_are_json_dumps_texts(self):
        payload = {1e-07: 1, math.nan: 2, 2**70: 3, np.float64(1.5): 4, _Float(2.5): 5}
        assert sorted(await run_jq_first("keys", payload)) == sorted(["1e-07", "NaN", str(2**70), "1.5", "2.5"])

    @pytest.mark.parametrize(
        ("key", "type_name"),
        [
            (datetime.datetime(2026, 1, 2), "datetime.datetime"),
            (datetime.date(2026, 1, 2), "datetime.date"),
            (uuid.UUID(int=1), "UUID"),
            (_Plain.X, "_Plain"),
            ((1, 2), "tuple"),
            (np.int64(3), "numpy.int64"),
        ],
        ids=["datetime", "date", "uuid", "plain_enum", "tuple", "numpy_int64"],
    )
    async def test_a_key_json_dumps_refuses_raises_its_text(self, key, type_name):
        with pytest.raises(TypeError) as exc:
            await run_jq_first(".", {"nested": {key: 1}})
        assert str(exc.value) == f"keys must be str, int, float, bool or None, not {type_name}"

    @pytest.mark.parametrize("order", ["key_first", "value_first"])
    async def test_an_int_key_beside_a_decimal_value_raises_the_decimal_text(self, order):
        items = [(1, "a"), ("d", decimal.Decimal("1"))]
        payload = dict(items if order == "key_first" else reversed(items))
        with pytest.raises(TypeError) as exc:
            await run_jq_first(".", payload)
        assert str(exc.value) == "Object of type Decimal is not JSON serializable"

    async def test_an_int_key_beside_a_plain_enum_value_raises_todays_text(self):
        with pytest.raises(TypeError) as exc:
            await run_jq_first(".", {1: "a", "e": _Plain.X})
        assert str(exc.value) == "Object of type _Plain is not JSON serializable"


def _nested_dicts(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"n": value}
    return value


def _nested_lists(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


class TestNestingDepth:
    @pytest.mark.parametrize("build", [_nested_dicts, _nested_lists], ids=["dicts", "lists"])
    async def test_a_deep_envelope_gives_todays_result(self, build):
        payload = build(300)
        assert await run_jq_first(".", payload) == _through_json_dumps(payload)

    @pytest.mark.parametrize("build", [_nested_dicts, _nested_lists], ids=["dicts", "lists"])
    async def test_a_moderately_deep_envelope_gives_todays_result(self, build):
        payload = build(200)
        assert await run_jq_first(".", payload) == _through_json_dumps(payload)


class TestJsonDumpsRunsOnlyOnTheFallbackTexts:
    @pytest.fixture
    def dumps_calls(self, monkeypatch):
        calls: list[Any] = []
        real = json.dumps

        def _spy(obj, *args, **kwargs):
            calls.append(obj)
            return real(obj, *args, **kwargs)

        monkeypatch.setattr(json, "dumps", _spy)
        return calls

    async def test_an_all_str_key_envelope_never_reaches_json_dumps(self, dumps_calls):
        await run_jq_first(".", {"a": [1, 2.5, "s", None, True], "b": _VALUES["namedtuple"]})
        await run_jq_first(".", _nested_dicts(200))
        await run_jq_bounded(".[]", [1, 2], limit=2)
        assert dumps_calls == []

    @pytest.mark.parametrize(
        "payload",
        [{1: "int key"}, {"big": 2**70}, _nested_dicts(300), _nested_lists(300)],
        ids=["non_str_key", "big_int", "deep_dicts", "deep_lists"],
    )
    async def test_a_fallback_text_sends_the_envelope_to_json_dumps_once(self, dumps_calls, payload):
        await run_jq_first(".", payload)
        assert len(dumps_calls) == 1
