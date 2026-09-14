"""Vendor-cell parsing: picking present-but-falsy values and coercing a metrics
measure cell to a non-negative finite number."""

from __future__ import annotations

import math
from typing import Any, TypeGuard


def _pick(raw: dict[str, Any], *keys: str) -> Any:
    """First of ``keys`` present in ``raw`` with a non-None value, else None.

    Preserves falsy-but-valid values (``0``, ``""``, ``[]``), unlike ``a or b``.
    """
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _nonneg_number(value: Any) -> TypeGuard[float]:
    """A real, non-negative number — the -1 sentinel a backend returns for an
    uncomputed metric is excluded."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _measure_key(row: dict[str, Any], measure: str) -> str | None:
    """The row's column for a summed measure. The backend names the output column
    by measure + aggregation (e.g. ``sum_totalTokens`` / ``totalTokens_sum``) and
    the exact key varies, so match by substring with an exact-key fast path.
    ``None`` when no column matches — presence is decided by the column, never by
    its value (a null sum is a real measure cell, not a missing column)."""
    if measure in row:
        return measure
    needle = measure.lower()
    for key in row:
        if isinstance(key, str) and needle in key.lower():
            return key
    return None


def _measure_value(cell: Any) -> float | None:
    """Parse a metrics measure cell to a non-negative finite float. Numbers and
    numeric strings (ClickHouse-backed sums serialise as strings) become floats;
    ``None`` and the empty string mean no usage and become ``None``. A bool, an
    unparseable string, or a non-finite/negative value is not a token sum —
    ``None`` (a token sum is a non-negative finite number)."""
    if isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        number = float(cell)
    elif isinstance(cell, str):
        text = cell.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number
