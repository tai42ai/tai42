"""The single per-scan allowance a condition draws from: the token count and nesting
depth bounds, and the mutable :class:`_Budget` threaded through lexer and parser."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import _refusal

# Analysis-REFUSAL bounds, not limits; headroom over the shipped conditions is small (pinned by test).
_MAX_TOKENS = 256
_MAX_NESTING_DEPTH = 32


@dataclass
class _Budget:
    """The SINGLE allowance one scan of one condition draws from.

    Threaded through the lexer, the parser and every ``\\(...)`` body they descend into,
    so an interpolation cannot mint a fresh allowance and the depth counter tracks the
    real interpreter stack.
    """

    tokens: int = 0
    depth: int = 0

    def spend(self, condition_text: str, position: int) -> None:
        """Account for one lexed token, refusing past :data:`_MAX_TOKENS`."""
        self.tokens += 1
        if self.tokens > _MAX_TOKENS:
            raise _refusal(
                condition_text,
                position,
                f"condition is longer than the {_MAX_TOKENS} tokens the token-free scan will analyze; shorten it "
                "or move part of it into a named scope/role",
            )

    def descend(self, condition_text: str, position: int) -> None:
        """Enter one level of nesting, refusing past :data:`_MAX_NESTING_DEPTH`. Always
        paired with a ``finally`` that calls :meth:`ascend`."""
        self.depth += 1
        if self.depth > _MAX_NESTING_DEPTH:
            raise _refusal(
                condition_text,
                position,
                f"condition nests more than {_MAX_NESTING_DEPTH} levels deep, past what the token-free scan will "
                "analyze",
            )

    def ascend(self) -> None:
        """Leave the level entered by :meth:`descend`."""
        self.depth -= 1
