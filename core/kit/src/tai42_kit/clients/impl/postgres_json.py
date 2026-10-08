"""The json/jsonb read loader of the kit's own Postgres pools.

``platform_json_loads`` returns exactly what ``json.loads`` returns for every input:
it parses with orjson and sends to ``json.loads`` every input orjson would read
differently. orjson turns an integer outside ``[-2**63, 2**64 - 1]`` into a float
with no error, and every such integer has at least 19 digits, so an input holding
a run of 19 or more digits is parsed by ``json.loads``; an input orjson refuses
(``NaN``, ``1e400``, a lone surrogate escape, nesting deeper than 1024) is parsed by
``json.loads`` too, which returns its value or raises its own error. Writes are not
touched: values are dumped by psycopg's default ``json.dumps``.
"""

import json
from typing import Any

import orjson
from psycopg import AsyncConnection
from psycopg.types.json import set_json_loads

__all__ = ["configure_platform_json", "platform_json_loads"]

# Maps every digit byte to ``d`` and every other byte to ``.``, so one C-level
# ``translate`` plus a substring search finds a run of 19 digits.
_DIGIT_MASK = bytes.maketrans(bytes(range(256)), bytes(b"d"[0] if 0x30 <= b <= 0x39 else b"."[0] for b in range(256)))
_DIGIT_RUN = b"d" * 19


def platform_json_loads(data: str | bytes) -> Any:
    """Parse one json/jsonb value exactly as ``json.loads`` does."""
    # A module-level function closing over nothing: psycopg caches the loader class
    # it builds for it by code object. psycopg's loaders pass bytes.
    raw = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else data
    if _DIGIT_RUN in raw.translate(_DIGIT_MASK):
        return json.loads(data)
    try:
        return orjson.loads(data)
    except orjson.JSONDecodeError:
        return json.loads(data)


async def configure_platform_json(conn: AsyncConnection[Any]) -> None:
    """Register :func:`platform_json_loads` for json and jsonb on ``conn`` only."""
    set_json_loads(platform_json_loads, conn)
