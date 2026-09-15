"""Route-declaration models and their overlap grammar.

``RouteDecl``/``RoutesDecl`` are the HTTP routes a plugin item declares; ``_route_shape`` and
``_shapes_overlap`` are the pure helpers a spec uses to detect a route collision.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

# One literal segment of a declared route path: filename-safe characters, no
# slash. A template segment (``{name}``) is matched separately.
ROUTE_LITERAL_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# One template segment of a declared route path: ``{name}`` where ``name`` is a
# python identifier. A Starlette converter suffix (``{x:path}``) is INVALID —
# plugin declarations never carry converters.
ROUTE_PARAM_SEGMENT_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")

# One segment of a route mount base: lowercase alphanumerics and interior
# hyphens, no leading/trailing hyphen. Segments join with ``/``; the base itself
# is relative (no leading/trailing slash) and carries no templates.
ROUTE_BASE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


RouteMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class RouteDecl(BaseModel):
    """One HTTP route a plugin item declares, relative to its mount base.

    ``path`` is ``/``-prefixed with at least one segment; each segment is a
    literal (:data:`ROUTE_LITERAL_SEGMENT_RE`) or a ``{name}`` template
    (:data:`ROUTE_PARAM_SEGMENT_RE`, a python identifier with no converter
    suffix). ``methods`` is a non-empty set of uppercase HTTP methods, unique
    within the row. ``public`` states — with no default, so the declaration is
    never silent — whether the resolved route answers unauthenticated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    methods: list[RouteMethod]
    public: bool

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"route path {value!r} must be '/'-prefixed")
        segments = value.split("/")[1:]
        if not segments or "" in segments:
            raise ValueError(f"route path {value!r} must have >=1 non-empty segment and no trailing slash")
        for segment in segments:
            if segment in (".", ".."):
                raise ValueError(
                    f"route path segment {segment!r} is a path-traversal token, never a valid route segment"
                )
            if "{" in segment or "}" in segment:
                if not ROUTE_PARAM_SEGMENT_RE.fullmatch(segment):
                    raise ValueError(
                        f"route path segment {segment!r} must be a '{{name}}' template with a "
                        "python-identifier name and no converter suffix"
                    )
            elif not ROUTE_LITERAL_SEGMENT_RE.fullmatch(segment):
                raise ValueError(
                    f"route path segment {segment!r} must be a literal ([a-zA-Z0-9._-]+) or a '{{name}}' template"
                )
        return value

    @field_validator("methods")
    @classmethod
    def _check_methods(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("methods must name at least one HTTP method")
        if len(set(value)) != len(value):
            raise ValueError("methods must be unique")
        return value


class RoutesDecl(BaseModel):
    """The route block one item declares: a default mount ``base`` and its rows.

    ``base`` is RELATIVE — one or more :data:`ROUTE_BASE_SEGMENT_RE` segments
    joined by ``/``, no leading/trailing slash, no templates. The resolved
    absolute path of each row is ``/api/`` + ``base`` + the row's ``path``; the
    ``/api/`` root is fixed platform-wide and only ``base`` is remappable.
    ``paths`` is non-empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base: str
    paths: list[RouteDecl]

    @field_validator("base")
    @classmethod
    def _check_base(cls, value: str) -> str:
        if value.startswith("/") or value.endswith("/"):
            raise ValueError(f"route base {value!r} must be relative (no leading or trailing '/')")
        segments = value.split("/")
        for segment in segments:
            if not ROUTE_BASE_SEGMENT_RE.fullmatch(segment):
                raise ValueError(
                    f"route base segment {segment!r} must match {ROUTE_BASE_SEGMENT_RE.pattern} (no templates)"
                )
        return value

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[RouteDecl]) -> list[RouteDecl]:
        if not value:
            raise ValueError("routes must declare at least one path")
        return value


def route_shape(base: str, path: str) -> tuple[str | None, ...]:
    """Resolved segment shape of one declared route for overlap comparison.

    The ``base`` segments followed by the ``path`` segments, each a literal text or
    ``None`` for a ``{name}`` template position. The fixed ``/api/`` root is a
    constant prefix on every route and omitted.
    """
    segments: list[str | None] = list(base.split("/"))
    segments.extend(None if seg.startswith("{") else seg for seg in path.split("/")[1:])
    return tuple(segments)


def shapes_overlap(a: tuple[str | None, ...], b: tuple[str | None, ...]) -> bool:
    """True when two resolved shapes can match one concrete request path.

    Requires equal segment count and, at every position, either side is a template or the
    two literals are equal (a concrete path instantiating a template IS an overlap).
    """
    if len(a) != len(b):
        return False
    return all(sa is None or sb is None or sa == sb for sa, sb in zip(a, b, strict=True))
