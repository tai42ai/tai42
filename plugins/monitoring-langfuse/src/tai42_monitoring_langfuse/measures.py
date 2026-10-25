"""Number checks for the cost, latency and metrics cells Langfuse returns."""

from __future__ import annotations

import math
from typing import Any, TypeGuard


def _nonneg_number(value: Any) -> TypeGuard[float]:
    """A real, non-negative number.

    The -1 sentinel a backend returns for an uncomputed metric is excluded.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _measure_value(cell: Any) -> float | None:
    """Parse a metrics measure cell to a non-negative finite float.

    Numbers and numeric strings (ClickHouse-backed sums serialise as strings) become
    floats; ``None`` and the empty string mean no usage and become ``None``. A bool, an
    unparseable string, or a non-finite/negative value is not a token sum — ``None`` (a
    token sum is a non-negative finite number).
    """
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
