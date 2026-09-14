"""Per-send form models and their against-schema validation.

``FormOption``/``FormData``/``FormPage`` are the per-send data layered over a form's
published schema; ``check_form_data``/``check_form_pages`` cross-check that data against
the schema (prefilled values, per-send option lists, and one-page-per-property coverage).
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, field_validator

_SCALAR_FORM_TYPES = ("string", "boolean", "integer", "number")


class FormOption(BaseModel):
    """One per-send choice for a form field: ``value`` is the string submitted as
    the answer, ``label`` (when set) is shown to the human in its place. A per-send
    option list REPLACES a property's schema ``enum`` for ONE send — the published
    form is unchanged, so a variant needs no re-publish. Frozen."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str | None = None

    @field_validator("value")
    @classmethod
    def _value_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form option value must be non-blank")
        return value

    @field_validator("label")
    @classmethod
    def _label_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("form option label must be non-blank when present")
        return value


class FormData(BaseModel):
    """Per-send data layered over a form's published schema for ONE send.

    ``values`` prefills top-level properties — each entry keyed by property name,
    its value shown filled in and validated against that property's schema.
    ``options`` supplies a per-send choice list for a property whose schema is a
    string (or an array of strings), keyed by property name: the list REPLACES that
    property's ``enum`` for this send only (labels shown, values submitted). The
    model holds only the shape; the cross-check against the schema (unknown
    property, a value that fails its schema, options on a non-string property, an
    empty list) is done once by the interaction request. Frozen."""

    model_config = ConfigDict(frozen=True)

    values: dict[str, Any] = {}
    options: dict[str, list[FormOption]] = {}


class FormPage(BaseModel):
    """One step of a stepped form: ``title`` heads the step and ``fields`` names the
    top-level properties shown on it. Across a form's ``pages`` every property
    appears exactly once (the interaction request enforces the coverage); absent
    ``pages`` means one page. Frozen."""

    model_config = ConfigDict(frozen=True)

    title: str
    fields: list[str]

    @field_validator("title")
    @classmethod
    def _title_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form page title must be non-blank")
        return value

    @field_validator("fields")
    @classmethod
    def _fields_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("form page fields must be a non-empty list")
        return value


def _schema_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # The form's top-level ``properties`` as a typed map of name -> property schema.
    # An absent / non-object ``properties`` yields an empty map; a property whose own
    # schema is not an object is dropped (a value/option keyed to it reads as unknown).
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, prop in cast("dict[Any, Any]", properties).items():
        if isinstance(prop, dict):
            result[str(name)] = cast("dict[str, Any]", prop)
    return result


def _form_option_values(prop: dict[str, Any], options: list[FormOption] | None) -> list[str] | None:
    # The allowed string set for a prefilled value: the per-send option values when a
    # per-send list is given (it replaces the enum for this send), else the property's
    # own ``enum`` — or None when the property constrains nothing.
    if options is not None:
        return [option.value for option in options]
    enum = prop.get("enum")
    if isinstance(enum, list):
        return [str(choice) for choice in cast("list[Any]", enum)]
    return None


def _form_property_is_stringish(prop: dict[str, Any]) -> bool:
    # A property a per-send option list may target: a string, or an array whose items
    # are strings. Any other property carries choices no single control can render.
    if prop.get("type") == "string":
        return True
    items = prop.get("items")
    return (
        prop.get("type") == "array"
        and isinstance(items, dict)
        and cast("dict[str, Any]", items).get("type") == "string"
    )


def _check_scalar_form_value(
    name: str, ptype: Any, value: Any, prop: dict[str, Any], options: list[FormOption] | None
) -> None:
    # Validate one prefilled ``value`` against a scalar property schema (string/boolean/
    # integer/number) plus any enum / per-send option constraint on a string. Raises
    # ``ValueError`` naming the field.
    if ptype == "string":
        if not isinstance(value, str):
            raise ValueError(f"form data value for {name!r} must be a string")
        allowed = _form_option_values(prop, options)
        if allowed is not None and value not in allowed:
            raise ValueError(f"form data value for {name!r} must be one of {allowed}")
    elif ptype == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be a boolean")
    elif ptype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be an integer")
    elif ptype == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be a number")


def _check_array_form_value(name: str, prop: dict[str, Any], value: Any, options: list[FormOption] | None) -> None:
    # Validate one prefilled ``value`` against an array-of-strings property (plus any
    # allowed-set constraint). Raises ``ValueError`` naming the field.
    items = cast("dict[str, Any]", prop["items"])
    if items.get("type") != "string":
        raise ValueError(f"form data value for {name!r} must be a list of strings")
    if not isinstance(value, list):
        raise ValueError(f"form data value for {name!r} must be a list of strings")
    value_list = cast("list[Any]", value)
    if not all(isinstance(item, str) for item in value_list):
        raise ValueError(f"form data value for {name!r} must be a list of strings")
    allowed = _form_option_values(items, options)
    if allowed is not None:
        bad = [item for item in value_list if item not in allowed]
        if bad:
            raise ValueError(f"form data value for {name!r} contains choices outside the allowed set: {bad}")


def _check_form_value(name: str, prop: dict[str, Any], value: Any, options: list[FormOption] | None) -> None:
    # Validate one prefilled ``value`` against its property's schema. A property without a
    # renderable scalar (or array-of-strings) type cannot be shown filled in, so it raises
    # rather than storing an unrenderable prefill. Raises ``ValueError`` naming the field.
    ptype = prop.get("type")
    if ptype in _SCALAR_FORM_TYPES:
        _check_scalar_form_value(name, ptype, value, prop, options)
    elif ptype == "array" and isinstance(prop.get("items"), dict):
        _check_array_form_value(name, prop, value, options)
    else:
        raise ValueError(
            f"form data cannot prefill {name!r}: its schema type {ptype!r} is not a renderable scalar "
            f"({', '.join(_SCALAR_FORM_TYPES)}) or an array of strings"
        )


def check_form_data(schema: dict[str, Any], data: FormData) -> None:
    """Validate a form's per-send :class:`FormData` against its schema: every
    ``values`` / ``options`` key is a declared top-level property, each prefilled
    value fits its property's schema, and a per-send option list targets only a
    string (or array-of-strings) property and is non-empty. Raises ``ValueError``
    naming the offending field."""
    props = _schema_properties(schema)
    for name, option_list in data.options.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"form data options names unknown property {name!r}")
        if not _form_property_is_stringish(prop):
            raise ValueError(f"form data options for {name!r} require a string (or array-of-strings) property")
        if not option_list:
            raise ValueError(f"form data options for {name!r} must be a non-empty list")
    for name, value in data.values.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"form data values names unknown property {name!r}")
        _check_form_value(name, prop, value, data.options.get(name))


def check_form_pages(schema: dict[str, Any], pages: list[FormPage]) -> None:
    """Validate a form's ``pages`` against its schema: every top-level property
    appears exactly once across the pages, and every named field is a declared
    property. Raises ``ValueError`` naming the missing / duplicate / unknown
    field."""
    declared = list(_schema_properties(schema))
    seen: list[str] = []
    for page in pages:
        for field in page.fields:
            if field not in declared:
                raise ValueError(f"form page {page.title!r} names unknown property {field!r}")
            if field in seen:
                raise ValueError(f"form page property {field!r} appears on more than one page")
            seen.append(field)
    missing = [name for name in declared if name not in seen]
    if missing:
        raise ValueError(f"form pages omit properties: {missing}")
