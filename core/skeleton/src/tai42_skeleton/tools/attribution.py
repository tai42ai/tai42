"""The generic run-attribution deposit seam + the attribution-wrap helper.

Every run's trace is tagged with its identity at the SAME seams the turn budget is armed
at — never a subset — because no single seam sees every run. This module owns the ambient
:class:`~tai42_contract.monitoring.RunAttribution` deposit (a ContextVar, mirroring the
execution-identity discipline) and the wrap helper that ENTERS
``attribute_run(writer, attribution)`` around the drive at each seam. The platform
interprets NOTHING the attribution carries.

A task created inside :func:`run_attribution` runs on a COPY and keeps the attribution for
its lifetime, exactly like the execution identity — so a detached fire's re-dispatch stays
attributed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from tai42_contract.monitoring import RunAttribution, attribute_run

_current_run_attribution: ContextVar[RunAttribution | None] = ContextVar("tai42_current_run_attribution", default=None)


def get_run_attribution() -> RunAttribution | None:
    """The attribution deposited for the current run, or ``None`` when none is set."""
    return _current_run_attribution.get()


def set_run_attribution(attribution: RunAttribution | None) -> Token[RunAttribution | None]:
    """Bind ``attribution`` as the current run's attribution; pass the returned token to
    :func:`reset_run_attribution` to restore the previous value."""
    return _current_run_attribution.set(attribution)


def reset_run_attribution(token: Token[RunAttribution | None]) -> None:
    """Restore the attribution to the value captured in ``token`` by the matching
    :func:`set_run_attribution` call."""
    _current_run_attribution.reset(token)


@contextmanager
def run_attribution(attribution: RunAttribution) -> Iterator[None]:
    """Deposit ``attribution`` as the ambient run attribution for the wrapped block,
    resetting it in a ``finally``. A task created inside the block inherits it on a copy."""
    token = set_run_attribution(attribution)
    try:
        yield
    finally:
        reset_run_attribution(token)


@contextmanager
def stamp_run_attribution() -> Iterator[None]:
    """Wrap the drive in the ambient attribution's trace scope, when one is deposited.

    Reads :func:`get_run_attribution`; when present, ENTERS
    ``attribute_run(get_monitoring().writer, attribution)`` so the drive's spans are
    created INSIDE the attribution scope; when absent, yields without wrapping. Fail-safe
    by construction — ``attribute_run`` no-ops outside a trace and its stamp catches +
    logs, never raises — so it cannot break a run. It NESTS harmlessly: an inner seam
    re-entering the scope around an already-attributed drive does not corrupt the trace.
    """
    attribution = get_run_attribution()
    if attribution is None:
        yield
        return
    from tai42_skeleton.monitoring import get_monitoring

    with attribute_run(get_monitoring().writer, attribution):
        yield
