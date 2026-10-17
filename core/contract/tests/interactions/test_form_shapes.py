"""The form-shape vocabulary: the shape keywords, the failing-shape kinds and the per-field error."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.interactions import SHAPE_KEYWORDS, FormShapeError, FormShapeKind


def test_shape_keywords_are_the_keywords_a_value_can_break():
    assert set(SHAPE_KEYWORDS) == {
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
    }


def test_form_shape_kind_vocabulary():
    assert {kind.value for kind in FormShapeKind} == {
        "required",
        "type",
        "pattern",
        "format",
        "min_length",
        "max_length",
        "minimum",
        "maximum",
        "min_items",
        "max_items",
        "enum",
        "min_date",
        "max_date",
        "date_unavailable",
        "range_order",
        "range_span",
        "other",
    }


def test_form_shape_error_is_frozen_and_strict():
    error = FormShapeError(
        field="qty", kind=FormShapeKind.MINIMUM, params={"minimum": 1}, message="Enter a number of at least 1"
    )
    assert error.model_dump(mode="json") == {
        "field": "qty",
        "kind": "minimum",
        "params": {"minimum": 1},
        "message": "Enter a number of at least 1",
    }
    with pytest.raises(ValidationError):
        error.field = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError, match=r"Extra inputs are not permitted"):
        FormShapeError.model_validate({"field": "a", "kind": "type", "params": {}, "message": "m", "nope": 1})
    with pytest.raises(ValidationError):
        FormShapeError(field="a", kind="not-a-kind", params={}, message="m")  # type: ignore[arg-type]
