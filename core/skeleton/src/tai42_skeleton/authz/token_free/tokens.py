"""The lexical token vocabulary the lexer produces and the parser consumes.

The token kinds, an interpolation span, the token record, and how a token reads in a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class _Kind(Enum):
    """The lexical classes the condition grammar is built from."""

    FIELD = "field"
    IDENT = "identifier"
    VARIABLE = "variable"
    NUMBER = "number"
    STRING = "string"
    FORMAT = "format"
    OPERATOR = "operator"
    END = "end"


@dataclass(frozen=True)
class _Interpolation:
    r"""A ``\\(...)`` hole in a string literal, as a half-open span of the condition text.

    Its body is jq CODE and is parsed and analyzed as such.
    """

    start: int
    end: int


@dataclass(frozen=True)
class _Token:
    kind: _Kind
    text: str
    position: int
    interpolations: tuple[_Interpolation, ...] = ()


def _describe(token: _Token) -> str:
    """How a token reads in a refusal message."""
    return "the end of the condition" if token.kind is _Kind.END else repr(token.text)
