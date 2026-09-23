"""The ``ask`` error types: the timeout and the open-question-limit refusals.

Each is tagged with the error kind the tool layer surfaces.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class InteractionTimeoutError(Exception):
    """Raised when ``ask`` gets no answer within its timeout budget."""

    # No answer arrived within the timeout budget.
    __tai_error_kind__ = ErrorKind.TIMED_OUT


class InteractionLimitError(Exception):
    """Raised when a new ``ask`` call is refused because too many questions are already open.

    Enforced by the ``max_concurrent`` guard.
    """

    # Judgment call: the open-question ceiling is a saturated resource, so the ask is
    # refused as UNAVAILABLE (a temporary refusal), not a caller BAD_INPUT.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE
