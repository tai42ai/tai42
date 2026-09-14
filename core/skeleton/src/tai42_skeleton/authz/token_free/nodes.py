"""The parsed-construct node types the parser builds and the taint analysis walks:
one frozen dataclass per grammar construct, each carrying its source offset."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Node:
    """A parsed construct, carrying the offset a refusal about it reports."""

    position: int


@dataclass(frozen=True)
class _Root(_Node):
    """``.`` — the value flowing into the current expression."""


@dataclass(frozen=True)
class _Constant(_Node):
    """A number or a ``true``/``false``/``null`` literal."""


@dataclass(frozen=True)
class _String(_Node):
    """A string literal. ``value`` is the decoded text and is a usable static field name
    only when ``interpolations`` is empty."""

    value: str
    interpolations: tuple[_Node, ...] = ()


@dataclass(frozen=True)
class _Variable(_Node):
    name: str


@dataclass(frozen=True)
class _Field(_Node):
    """A ``.name`` / ``."name"`` projection suffix."""

    name: str


@dataclass(frozen=True)
class _Index(_Node):
    """A ``[expr]`` suffix, or ``[]`` (iterate all values) when ``key`` is ``None``."""

    key: _Node | None


@dataclass(frozen=True)
class _Optional(_Node):
    """A ``?`` suffix: the same value, with errors suppressed."""


@dataclass(frozen=True)
class _Projection(_Node):
    """``source`` with a chain of suffixes applied to it."""

    source: _Node
    suffixes: tuple[_Field | _Index | _Optional, ...]


@dataclass(frozen=True)
class _Call(_Node):
    """An application of an allowlisted builtin to the current input."""

    name: str
    arguments: tuple[_Node, ...] = ()


@dataclass(frozen=True)
class _Binary(_Node):
    operator: str
    left: _Node
    right: _Node


@dataclass(frozen=True)
class _Negate(_Node):
    operand: _Node


@dataclass(frozen=True)
class _Bind(_Node):
    """``source as $variable | body``."""

    source: _Node
    variable: str
    body: _Node


@dataclass(frozen=True)
class _Try(_Node):
    """``try body`` with an optional ``catch handler``."""

    body: _Node
    handler: _Node | None


@dataclass(frozen=True)
class _ObjectConstruction(_Node):
    """``{key: value, ...}``. The keys are static names, so only the VALUES can carry a
    value derived from the auth context."""

    values: tuple[_Node, ...] = ()


@dataclass(frozen=True)
class _ArrayConstruction(_Node):
    """``[expr]``, or ``[]`` when ``element`` is ``None``."""

    element: _Node | None
