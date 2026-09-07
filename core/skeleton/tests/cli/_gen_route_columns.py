"""Derive the CLI's route->column table from each route's ``response_model``.

The ``tai42-cli`` package is the standalone remote client and is banned from
importing ``tai42_skeleton`` (where the response models live), so it cannot read
the route registry at runtime. Instead the table is DERIVED here — the skeleton
test tree, where both packages are importable — and shipped into the CLI as the
generated ``tai42_cli.commands._route_columns`` module. The drift gate in
``test_route_columns.py`` re-derives it and fails if the committed table falls out
of sync; run this module as a script to regenerate that committed file.

A route is a table entry only when its ``response_model`` yields a flat list to
tabulate: a ``RootModel[list[X]]`` (the body IS the list) or an envelope with exactly
one list-typed field. The columns are the row model's field names in declaration
order, or ``("value",)`` for a bare-scalar row. A model with no list field (a
single-object result), more than one list field, or a list of open objects
(``dict``/``JsonValue``) / unions is NOT auto-derivable — no entry is emitted and the
command keeps an explicit column list.

A bare-scalar list beside the model's own non-pagination scalar fields is NOT a list
envelope but a SUMMARY object carrying an incidental list (an operation receipt or a
single record that happens to hold a list of ids/names). Rendered as a ``("value",)``
column it would drop those scalar fields, so such a model is emitted with
``items_key=None`` and columns of ALL its fields. A one-list envelope keeps its
``items_key`` when its other fields are all pagination metadata (the allowlist below)
or its rows are structured (a row MODEL, whose own fields are the columns — the
envelope's counts never belonged in the table).
"""

from __future__ import annotations

import types
import typing
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, RootModel

from tai42_skeleton.app.route_registry import load_api_routes

_SCALAR_TYPES = (str, int, float, bool)

# Field names that count or paginate an envelope's rows rather than describe a summary;
# their presence beside a bare-scalar list does not make the model a summary object.
_PAGINATION_FIELDS = frozenset({"total", "page", "page_size", "count", "next", "next_page", "next_cursor", "has_more"})


class RouteShape(NamedTuple):
    """The table shape ``emit_records`` renders for one route: the envelope list
    field (``None`` when the body itself is the list) and the row columns."""

    items_key: str | None
    columns: tuple[str, ...]


def _unwrap_annotated(annotation: Any) -> Any:
    """Strip an ``Annotated[...]`` wrapper, leaving the underlying type."""
    if getattr(annotation, "__metadata__", None) is not None:
        return typing.get_args(annotation)[0]
    return annotation


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _row_columns(item_type: Any) -> tuple[str, ...] | None:
    """The columns for a list whose element is ``item_type``, or ``None`` when the
    element is an open object (``dict``/``JsonValue``), a union, or a nested
    ``RootModel`` — a row shape the table cannot flatten."""
    item_type = _unwrap_annotated(item_type)
    if _is_model(item_type):
        if issubclass(item_type, RootModel):
            return None
        return tuple(item_type.model_fields)
    if item_type in _SCALAR_TYPES:
        return ("value",)
    return None


def _list_element(annotation: Any) -> Any | None:
    """The element type of ``annotation`` when it is a ``list[...]``, else ``None``."""
    annotation = _unwrap_annotated(annotation)
    if typing.get_origin(annotation) is list:
        return typing.get_args(annotation)[0]
    return None


def _is_scalar(annotation: Any) -> bool:
    """Whether ``annotation`` renders as a single cell: a ``str``/``int``/``float``/
    ``bool`` or an ``Optional``/union of them (``int | None``)."""
    annotation = _unwrap_annotated(annotation)
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return bool(members) and all(_is_scalar(member) for member in members)
    return annotation in _SCALAR_TYPES


def _has_non_pagination_scalar(model: type[BaseModel], list_name: str) -> bool:
    """Whether ``model`` carries a scalar field other than its ``list_name`` list and the
    pagination-metadata allowlist — the mark of a summary object rather than a plain
    list-of-scalars envelope whose only extra fields count or paginate the rows."""
    return any(
        name != list_name and name not in _PAGINATION_FIELDS and _is_scalar(field.annotation)
        for name, field in model.model_fields.items()
    )


def _shape_of(model: type[BaseModel]) -> RouteShape | None:
    """Distil the table shape from a response model, or ``None`` when the body is
    not a single flat list (a single object, a multi-list envelope, or an open
    body). Raises on a structurally unexpected model rather than guessing."""
    if issubclass(model, RootModel):
        root = model.model_fields.get("root")
        if root is None:
            raise ValueError(f"{model.__name__}: RootModel carries no 'root' field")
        element = _list_element(root.annotation)
        if element is None:
            # RootModel[dict[...]] / RootModel[JsonValue] and friends: an opaque body.
            return None
        columns = _row_columns(element)
        return None if columns is None else RouteShape(items_key=None, columns=columns)
    list_fields = [
        (name, element)
        for name, field in model.model_fields.items()
        if (element := _list_element(field.annotation)) is not None
    ]
    if len(list_fields) != 1:
        # Zero list fields is a single-object result (emit_result renders it whole);
        # more than one is a multi-list envelope the command selects from locally.
        return None
    name, element = list_fields[0]
    columns = _row_columns(element)
    if columns is None:
        return None
    if _is_scalar(element) and _has_non_pagination_scalar(model, name):
        # A bare-scalar list beside the model's own non-pagination scalar fields: a
        # summary object carrying an incidental list, whose ``("value",)`` column would
        # hide those fields. Surface every field under the whole-object shape instead.
        return RouteShape(items_key=None, columns=tuple(model.model_fields))
    return RouteShape(items_key=name, columns=columns)


def derive_shapes() -> dict[tuple[str, str], RouteShape]:
    """The ``(METHOD, PATH) -> RouteShape`` table for every route whose response
    model yields a single flat list to tabulate."""
    shapes: dict[tuple[str, str], RouteShape] = {}
    for meta in load_api_routes():
        if meta.response_model is None:
            continue
        shape = _shape_of(meta.response_model)
        if shape is None:
            continue
        for method in meta.methods:
            shapes[(method, meta.path)] = shape
    return shapes


_MODULE_HEADER = '''\
"""Generated route->column shapes for the CLI's table renderer.

DO NOT EDIT BY HAND. Each entry is derived from a route's declared response model
by ``core/skeleton/tests/cli/_gen_route_columns.py``; the drift gate in
``core/skeleton/tests/cli/test_route_columns.py`` fails if this table falls out of
sync with the models. Regenerate with ``python core/skeleton/tests/cli/_gen_route_columns.py``.

``emit_records`` reads a route's shape here: ``items_key`` is the envelope's list
field (``None`` when the body itself is the list) and ``columns`` are the row
model's field names in declaration order (``("value",)`` for a bare-scalar row).
"""

from __future__ import annotations

from typing import NamedTuple


class RouteShape(NamedTuple):
    items_key: str | None
    columns: tuple[str, ...]


ROUTE_TABLE_SHAPES: dict[tuple[str, str], RouteShape] = {
'''


def render_module_source(shapes: dict[tuple[str, str], RouteShape]) -> str:
    """The full source text of the committed ``_route_columns.py`` module."""
    lines = [_MODULE_HEADER]
    for method, path in sorted(shapes):
        shape = shapes[(method, path)]
        inner = ", ".join(f'"{column}"' for column in shape.columns)
        columns = f"({inner},)" if len(shape.columns) == 1 else f"({inner})"
        items = "None" if shape.items_key is None else f'"{shape.items_key}"'
        lines.append(f'    ("{method}", "{path}"): RouteShape(items_key={items}, columns={columns}),\n')
    lines.append("}\n")
    return "".join(lines)


def _committed_path() -> Path:
    import tai42_cli

    return Path(tai42_cli.__file__).parent / "commands" / "_route_columns.py"


def write_committed() -> None:
    """(Re)write the shipped ``_route_columns.py`` from the live registry."""
    _committed_path().write_text(render_module_source(derive_shapes()))


if __name__ == "__main__":
    write_committed()
    print(f"wrote {_committed_path()}")
