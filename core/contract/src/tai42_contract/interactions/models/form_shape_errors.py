"""The vocabulary of a form field whose value breaks its declared shape.

``SHAPE_KEYWORDS`` are the form-schema keywords that declare a shape a value can break;
``FormShapeKind`` names which shape a value breaks; ``FormShapeError`` is one field's broken
shape with the bound it names and the platform's plain sentence for it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

#: The form-schema keywords that declare a SHAPE a field's value can break — the JSON-Schema string,
#: number and array bounds, ``pattern`` and ``format``, the platform date constraints, and the
#: ``timeOf`` of a range end's time (the END's time can break the range order). A field carrying
#: none of them (on itself or on an array's ``items``) has no shape to word an error for.
SHAPE_KEYWORDS: tuple[str, ...] = (
    "pattern",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "minDate",
    "maxDate",
    "unavailableDates",
    "rangeStart",
    "minDays",
    "maxDays",
    "timeOf",
)


class FormShapeKind(StrEnum):
    """Which declared shape a form field's value breaks — the kind a surface words its error by.

    ``required`` a demanded field is missing; ``type`` the value is not the declared type; the
    keyword kinds name the JSON-Schema keyword broken; ``min_date``/``max_date``/
    ``date_unavailable``/``range_order``/``range_span`` name the platform date constraint broken
    (``range_order`` is keyed to the END date, or to the END's ``timeOf`` time when the two dates
    are equal and the times decide; its ``params["by"]`` reads ``"date"`` or ``"time"``);
    ``other`` is any other keyword (its name rides the error's ``params["keyword"]``).
    """

    REQUIRED = "required"
    TYPE = "type"
    PATTERN = "pattern"
    FORMAT = "format"
    MIN_LENGTH = "min_length"
    MAX_LENGTH = "max_length"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    MIN_ITEMS = "min_items"
    MAX_ITEMS = "max_items"
    ENUM = "enum"
    MIN_DATE = "min_date"
    MAX_DATE = "max_date"
    DATE_UNAVAILABLE = "date_unavailable"
    RANGE_ORDER = "range_order"
    RANGE_SPAN = "range_span"
    OTHER = "other"


class FormShapeError(BaseModel):
    """One form field whose value breaks its declared shape. Frozen.

    ``field`` is the top-level property name; ``kind`` the shape broken; ``params`` the bound the
    shape names (``{"minimum": 1}``, ``{"format": "email"}``, ``{"keyword": "multipleOf"}`` for
    ``other``); ``message`` the platform's plain sentence for it. A surface shows the caller's
    ``wording.form.fields[field].required`` for a ``required`` error and
    ``wording.form.fields[field].invalid`` for any other kind, when set, else its own word for
    ``kind`` or ``message``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    kind: FormShapeKind
    params: dict[str, Any]
    message: str
