"""The platform's json/jsonb read loader returns exactly what ``json.loads`` returns.

The loader parses with orjson, and sends to ``json.loads`` every input with a run of
19 or more digits (orjson turns integers outside 64 bits into floats) and every input
orjson refuses, so each value and each error is the standard library's.
"""

import json
import math
from typing import Any

import pytest

pytest.importorskip("psycopg")

from tai42_kit.clients.impl.postgres_json import platform_json_loads


def _record(size: int) -> dict[str, Any]:
    return {
        "id": "A-42",
        "name": "alice",
        "flags": [True, False, None],
        "counts": list(range(size)),
        "nested": {"ratio": 0.125, "label": "café ☕", "empty": {}, "list": []},
    }


def _nested(depth: int) -> str:
    return "[" * depth + "1" + "]" * depth


_CORPUS: list[bytes] = [
    json.dumps(_record(10)).encode(),
    json.dumps(_record(2000)).encode(),
    b"null",
    b"true",
    b'"text"',
    b"{}",
    b"[]",
    b'{"a": null, "b": [null, null]}',
    str(2**63 - 1).encode(),
    str(-(2**63 - 1)).encode(),
    str(-(2**63)).encode(),
    str(-(2**63) - 1).encode(),
    str(2**64 - 1).encode(),
    str(2**64).encode(),
    str(10**30).encode(),
    str(-(10**30)).encode(),
    b'{"big": 18446744073709551616, "small": 1}',
    b'{"neg": -9223372036854775809}',
    b"1234567890123456789",
    b"12345678901234567890",
    b'"1234567890123456789012345"',
    b'{"id": "12345678901234567890", "n": 3}',
    b"0.1",
    b"1e10",
    b"-0.0",
    b"3.141592653589793",
    b"1.7976931348623157e308",
    b"5e-324",
    b"NaN",
    b"Infinity",
    b"-Infinity",
    b"1e400",
    b'"\\ud800"',
    b'"\\udc00 tail"',
    b'"\\u0000"',
    b'{"key\\u0000": "v\\u0000"}',
    '"unicode: ünïcödé 日本語 🙂"'.encode(),
    b'{"dup": 1, "dup": 2}',
    _nested(10).encode(),
    _nested(1100).encode(),
]


def _outcome(fn: Any, data: bytes) -> tuple[str, Any]:
    try:
        return ("value", fn(data))
    except Exception as exc:
        return ("error", type(exc))


def _same_leaf(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left):
            return math.isnan(right)
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    return type(left) is type(right) and left == right


def _same(left: Any, right: Any) -> bool:
    """Equal values of equal types at every position, walked without recursion (deep inputs)."""
    pending = [(left, right)]
    while pending:
        a, b = pending.pop()
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            if list(a) != list(b):
                return False
            pending.extend((a[k], b[k]) for k in a)
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            pending.extend(zip(a, b, strict=True))
        elif not _same_leaf(a, b):
            return False
    return True


@pytest.mark.parametrize("data", _CORPUS, ids=lambda data: data[:40].decode(errors="replace"))
def test_parity_with_json_loads(data: bytes) -> None:
    expected = _outcome(json.loads, data)
    actual = _outcome(platform_json_loads, data)

    assert actual[0] == expected[0]
    if expected[0] == "error":
        assert actual[1] is expected[1]
    else:
        assert _same(actual[1], expected[1])


def test_integers_beyond_64_bits_keep_their_exact_value() -> None:
    assert platform_json_loads(b"18446744073709551616") == 2**64
    assert platform_json_loads(b"-9223372036854775809") == -(2**63) - 1
    assert type(platform_json_loads(b"-9223372036854775809")) is int
    assert platform_json_loads(b'{"v": 1000000000000000000000000000000}') == {"v": 10**30}


def test_the_loader_is_a_closure_free_module_function() -> None:
    # psycopg caches its loader subclass by the function's code object only when the
    # function closes over nothing.
    assert platform_json_loads.__closure__ is None
    assert platform_json_loads.__module__ == "tai42_kit.clients.impl.postgres_json"


def test_a_text_input_parses_like_its_bytes() -> None:
    assert platform_json_loads('{"v": 18446744073709551616}') == {"v": 2**64}
    assert platform_json_loads('{"v": [1, 2]}') == {"v": [1, 2]}
    assert platform_json_loads('"\ud800"') == json.loads('"\ud800"')
