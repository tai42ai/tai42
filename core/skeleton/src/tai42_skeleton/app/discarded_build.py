"""The known state an epoch build discarded before its swap leaves, and the reload statement of it.

A failing epoch build is discarded before any commit step runs
(:func:`tai42_skeleton.app.epoch.build_and_swap_epoch`): the previous epoch keeps serving under
the env the discard restored. The failure re-raised once the discard completed is marked here
(:func:`mark_discarded_build`). A reload that failed that way states, on the failure, the
recycle-class drift as it stands after the discard (:func:`state_discarded_build`): the same
measurement an applied reload's reply carries. A worker's ``failed`` reload reply carries that
statement (:func:`discarded_build_statement`), and the recycle decision reads it by the same list
rule as an applied reply (:func:`states_discarded_build`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MARK = "_tai42_discarded_epoch_build"
"""The attribute :func:`mark_discarded_build` sets on a failure."""

_DRIFT = "_tai42_discarded_build_drift"
"""The attribute :func:`state_discarded_build` sets on a marked failure: the drift it measured."""

DISCARDED_BUILD_STATUS = "discarded"
"""The ``status`` a ``failed`` reload reply's payload carries when the worker's epoch build was discarded."""


def mark_discarded_build(failure: BaseException) -> None:
    """Mark ``failure`` as the failure of an epoch build discarded before its swap, its discard complete."""
    vars(failure)[_MARK] = True


def is_discarded_build_failure(failure: BaseException) -> bool:
    """Whether ``failure`` is the failure of an epoch build that was discarded before its swap.

    True only for the failure the epoch build re-raises once its discard has completed: no
    commit step ran, and the previous epoch keeps serving under the env the discard restored.
    Any other failure — one raised before the build, by the commit, the swap, the retire or the
    background loops, or by the discard itself — is not one.
    """
    return vars(failure).get(_MARK) is True


def state_discarded_build(failure: BaseException, awaiting_respawn: list[str]) -> None:
    """State on the marked ``failure`` the recycle-class drift its reload left standing after the discard.

    ``failure`` is a discarded build's (:func:`is_discarded_build_failure`); ``awaiting_respawn``
    is the measurement taken after the discard: the recycle-class keys whose value differs from
    the one the process bound them under.
    """
    vars(failure)[_DRIFT] = tuple(awaiting_respawn)


def discarded_build_statement(failure: BaseException) -> dict[str, Any] | None:
    """The payload of a ``failed`` reload reply whose reload failed with ``failure``.

    ``{"status": "discarded", "awaiting_respawn": [...]}`` when the reload stated its discarded
    build's drift on ``failure`` (:func:`state_discarded_build`): the worker serves its previous
    epoch under the env the discard restored, with that drift standing. ``None`` for any other
    failure, whose reply leaves the worker's state unknown.
    """
    drift = vars(failure).get(_DRIFT)
    return None if drift is None else {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": list(drift)}


def states_discarded_build(payload: object) -> bool:
    """Whether a ``failed`` reload reply's ``payload`` states the worker's epoch build was discarded before its swap."""
    return isinstance(payload, Mapping) and payload.get("status") == DISCARDED_BUILD_STATUS
