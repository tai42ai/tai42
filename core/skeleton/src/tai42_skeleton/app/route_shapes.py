"""Path SHAPE algebra for one-owner-per-route enforcement.

A route's shape is its sequence of segments with the literal text erased from
every template position, so two routes that can match the SAME concrete request
have overlapping shapes. The registry indexes every ``/api`` route by shape to
answer two questions with one algebra: does a newly registered route COLLIDE
with an existing one of a different owner (§ registration), and which registered
route OWNS a concrete request path (§ the verifier's declared-public tier).

Three segment kinds:

* :class:`Literal` — a fixed path segment, overlaps only the identical literal.
* :class:`Param` — a single-segment template (``{name}`` or a Starlette converter
  other than ``:path``), overlaps any one segment.
* :class:`PathParam` — a Starlette ``{name:path}`` rest-converter. It swallows a
  middle span of any length (>= 0); the fixed segments BEFORE it and any literal
  or template segments AFTER it (a ``:path`` need not be terminal) must still
  align positionally, so a shape too short for that fixed frame does not overlap.
  A plugin declaration can never carry a converter (the contract forbids it), so
  a ``PathParam`` only ever appears in a CORE registered route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# One template segment as registered: ``{name}`` or ``{name:converter}``. The
# ``:path`` converter is the rest-matcher handled as :class:`PathParam`; every
# other converter (``:int``, ``:str``, ``:uuid``, none) matches a single segment.
_TEMPLATE_SEGMENT_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*(?::([A-Za-z_][A-Za-z0-9_]*))?\}$")


@dataclass(frozen=True)
class Literal:
    """A fixed path segment; overlaps only the identical literal."""

    text: str


@dataclass(frozen=True)
class Param:
    """A single-segment template position; overlaps any one segment."""


@dataclass(frozen=True)
class PathParam:
    """A Starlette ``{name:path}`` rest-converter; overlaps any suffix."""


Segment = Literal | Param | PathParam
Shape = tuple[Segment, ...]


def parse_shape(path: str) -> Shape:
    """The shape of a registered path.

    ``path`` is ``/``-prefixed; its segments become :class:`Literal` for a fixed
    segment, :class:`Param` for a single-segment template, and :class:`PathParam`
    for a ``{name:path}`` rest-converter. The root path ``/`` is the empty shape.
    """
    segments: list[Segment] = []
    for raw in path.split("/"):
        if raw == "":
            continue
        template = _TEMPLATE_SEGMENT_RE.match(raw)
        if template is None:
            segments.append(Literal(raw))
        elif template.group(1) == "path":
            segments.append(PathParam())
        else:
            segments.append(Param())
    return tuple(segments)


def parse_concrete(path: str) -> Shape:
    """The shape of a CONCRETE request path — every segment a :class:`Literal`.

    A concrete request path carries no route templates, so a ``{name}`` (or a
    percent-decoded ``{name:path}``) segment is a literal, never a template
    position. ``path`` is ``/``-prefixed; the root path ``/`` is the empty shape.
    """
    return tuple(Literal(raw) for raw in path.split("/") if raw != "")


def overlap(a: Shape, b: Shape) -> bool:
    """Whether two shapes can match one concrete request path.

    Without a ``PathParam``: equal segment count and, at every position, either
    side a template or the two literals equal. With a ``PathParam`` the
    rest-converter swallows a middle span of any length (>= 0) while the fixed
    PREFIX before it and the fixed SUFFIX after it must still align positionally
    against the other shape — a ``:path`` need not be the final segment.

    Assumes at most one ``PathParam`` per shape: plugin routes carry none (the
    contract forbids converters) and core routes carry at most one, so only the
    first is consulted; two rest-converters in one shape are outside this algebra.
    """
    a_rest = _path_param_index(a)
    b_rest = _path_param_index(b)
    if a_rest is None and b_rest is None:
        if len(a) != len(b):
            return False
        return all(_segments_overlap(sa, sb) for sa, sb in zip(a, b, strict=True))

    if a_rest is not None and b_rest is not None:
        # Both rests swallow arbitrary middles, so the shapes overlap when their
        # fixed prefixes agree up to the earlier rest AND their fixed suffixes,
        # anchored at the end, agree over their common length.
        prefix = min(a_rest, b_rest)
        if not all(_segments_overlap(a[i], b[i]) for i in range(prefix)):
            return False
        return all(
            _segments_overlap(sa, sb)
            for sa, sb in zip(reversed(a[a_rest + 1 :]), reversed(b[b_rest + 1 :]), strict=False)
        )

    # Exactly one side has a rest-converter: the other is a fixed-length shape
    # that must span the prefix and suffix framing the rest, with the middle free.
    if a_rest is not None:
        rest_shape, rest_index, plain = a, a_rest, b
    else:
        assert b_rest is not None
        rest_shape, rest_index, plain = b, b_rest, a
    suffix = rest_shape[rest_index + 1 :]
    if len(plain) < rest_index + len(suffix):
        return False
    if not all(_segments_overlap(plain[i], rest_shape[i]) for i in range(rest_index)):
        return False
    return all(_segments_overlap(sp, ss) for sp, ss in zip(reversed(plain), reversed(suffix), strict=False))


def collision(a: Shape, a_methods: frozenset[str], b: Shape, b_methods: frozenset[str]) -> bool:
    """Whether two routes collide: their shapes overlap AND their method sets
    intersect. Same shape with disjoint methods is not a collision."""
    return bool(a_methods & b_methods) and overlap(a, b)


def _segments_overlap(a: Segment, b: Segment) -> bool:
    if isinstance(a, Literal) and isinstance(b, Literal):
        return a.text == b.text
    # ``overlap`` only pairs positions outside a rest-converter's swallowed middle,
    # so here at least one side is a template that matches any single segment.
    return True


def _path_param_index(shape: Shape) -> int | None:
    """The index of the shape's FIRST ``PathParam`` rest-converter (a ``:path``
    need not be terminal — core routes carry literals after it), or ``None`` when
    the shape has none."""
    for index, segment in enumerate(shape):
        if isinstance(segment, PathParam):
            return index
    return None
