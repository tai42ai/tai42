"""A float written as text the way JavaScript writes it."""

import math
from decimal import Decimal


def number_text(value: float) -> str:
    """A float as JavaScript's ``Number.prototype.toString`` writes it (``10``, ``0.00005``, ``1e+21``).

    The shortest digits that round-trip, so no value is lost; a non-finite float has no text and raises
    ``ValueError``.
    """
    # ECMAScript Number::toString(x) for a finite float: digits s (k of them) with x = s * 10**(n - k).
    if not math.isfinite(value):
        raise ValueError(f"a number must be finite to have text, got {value!r}")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + number_text(-value)
    decimal = Decimal(repr(value)).normalize()
    digits = "".join(str(digit) for digit in decimal.as_tuple().digits)
    k = len(digits)
    n = int(decimal.as_tuple().exponent) + k
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    exponent = f"e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"
    return digits + exponent if k == 1 else f"{digits[0]}.{digits[1:]}{exponent}"
