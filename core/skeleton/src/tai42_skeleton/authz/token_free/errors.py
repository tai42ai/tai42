"""The refusal shape every token-free scan stage shares: the error type and the
bounded excerpt each refusal quotes to locate the offending construct."""

from __future__ import annotations

_EXCERPT_LENGTH = 32


class TokenFreeConditionError(Exception):
    """A jq policy condition cannot be shown evaluable by a background execution.

    Raised by :func:`~tai42_skeleton.authz.token_free.assert_token_free_evaluable`
    naming the offending construct and where it sits, and by
    :func:`~tai42_skeleton.authz.execution.assert_execution_key_evaluable` when a
    condition does not render at all.
    """


def _excerpt(condition_text: str, position: int) -> str:
    """A bounded, readable slice of ``condition_text`` starting at the offending
    construct — enough to locate it without echoing the whole condition."""
    return condition_text[position : position + _EXCERPT_LENGTH]


def _refusal(condition_text: str, position: int, detail: str) -> TokenFreeConditionError:
    """The one refusal shape: what was refused, and where to find it."""
    return TokenFreeConditionError(f"{detail} at offset {position} ({_excerpt(condition_text, position)!r})")
