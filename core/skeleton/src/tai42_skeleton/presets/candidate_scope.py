"""A scoped override that makes an UNSAVED preset body visible to the active-body read.

A bound-routes re-check (``operations.presets.references._assert_bound_routes_still_bind``) judges a
preset write BEFORE it commits. A route bound to a preset that COMPOSES the changed preset ``P`` is
re-validated against ``P``'s candidate version, but the owner validator resolves the composed preset
``P`` through the same preset-store read (:meth:`PresetStoreView.get_active_body`) every composed-preset
resolution uses — which otherwise reads the still-committed OLD body. This scope maps ``P`` to its
candidate body for the duration of the re-check, so that read returns the candidate; it is cleared on
exit and must never leak into the commit or any other read.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_contract.presets import PresetBody

_candidate_bodies: ContextVar[Mapping[str, PresetBody] | None] = ContextVar("preset_candidate_bodies", default=None)


def candidate_body_override(name: str) -> PresetBody | None:
    """The candidate body overriding ``name``'s active body in the current scope, or ``None``.

    ``None`` outside a re-check scope, or for a name the scope does not override.
    """
    overrides = _candidate_bodies.get()
    if overrides is None:
        return None
    return overrides.get(name)


@contextmanager
def candidate_bodies(overrides: Mapping[str, PresetBody]) -> Iterator[None]:
    """Make ``overrides`` (preset name -> unsaved candidate body) the answer the active-body read gives.

    For the duration of the ``with`` block, :meth:`PresetStoreView.get_active_body` returns the
    candidate body for an overridden name, so a composing route's composed-preset resolution sees the
    version a write would commit rather than the committed one. The override is reset on exit (including
    on an exception), so it never leaks into the actual commit or any later read.
    """
    token = _candidate_bodies.set(dict(overrides))
    try:
        yield
    finally:
        _candidate_bodies.reset(token)
