"""The entry-params policy a channel door applies when the enrichment it carries breaches the transport bounds.

The bounds themselves are contract vocabulary (:mod:`tai42_contract.entry_params`). A door picks how
it answers a breach: a door that answers a waiting caller REFUSES (the breach is that caller's
error), a webhook door whose vendor redelivers a 5xx forever DROPS the set and keeps the turn. Every
drop is logged at WARNING and counted on ``tai_entry_params_dropped_total{door, unit}``, never silent,
and neither a log line nor an error ever carries a param value.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING

from tai42_contract.entry_params import ENTRY_PARAM_VALUE_MAX_CHARS, validate_entry_params

if TYPE_CHECKING:
    from prometheus_client import Counter

logger = logging.getLogger(__name__)

__all__ = ["OverBoundParamsMode", "bound_entry_params", "put_entry_param"]


class OverBoundParamsMode(StrEnum):
    """How a door answers entry params that breach a transport bound."""

    REFUSE = "refuse"  # the door answers a waiting caller: the breach is that caller's error
    DROP = "drop"  # a webhook door whose vendor redelivers a 5xx forever: drop the set, keep the turn


@lru_cache(maxsize=1)
def _dropped_counter() -> Counter:
    """The process-wide drop counter, on the library's default registry.

    Created on first use, never at module import: the library picks its value backend at its own
    first import, and a serving process sets the multiprocess directory before that happens.
    """
    from prometheus_client import Counter

    return Counter("tai_entry_params_dropped", "Entry params a door dropped over a transport bound", ["door", "unit"])


def bound_entry_params(
    params: Mapping[str, str] | None, *, mode: OverBoundParamsMode, door: str
) -> dict[str, str] | None:
    """The params a door carries on: ``None`` when empty, a copy within the bounds, else per ``mode``.

    Over a bound, REFUSE re-raises the contract validator's ``ValueError`` unchanged (it names the
    bound and at most a key, never a value); DROP logs one WARNING naming the door and the bound,
    counts one dropped set and returns ``None`` so the turn proceeds without params.
    """
    if not params:
        return None
    candidate = dict(params)
    try:
        validate_entry_params(candidate)
    except ValueError as exc:
        if mode is OverBoundParamsMode.REFUSE:
            raise
        logger.warning("%s entry params dropped (%s); the turn proceeds without params", door, exc)
        _dropped_counter().labels(door=door, unit="set").inc()
        return None
    return candidate


def put_entry_param(params: dict[str, str], key: str, value: object, *, door: str) -> None:
    """Add ``key`` iff ``value`` is a non-empty string within the per-value cap.

    A non-string or empty value is absent (nothing to carry). An over-cap value is DROPPED, never
    truncated (truncating an opaque token silently corrupts it): one WARNING names the door, the key
    and the cap — never the value — and one dropped value is counted.
    """
    if not isinstance(value, str) or not value:
        return
    if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
        logger.warning(
            "%s entry param %r dropped: value over the %d-character limit", door, key, ENTRY_PARAM_VALUE_MAX_CHARS
        )
        _dropped_counter().labels(door=door, unit="value").inc()
        return
    params[key] = value
