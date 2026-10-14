"""The per-send ``flow_action_payload.data`` a form ask carries.

Each field's prefilled ``init`` value (shaped for its control) and each choice
field's per-send ``data-source`` option list, keyed by the identifier-safe
component name the published Flow uses. A merged ``rangeStart`` pair carries one
object ``init`` under its START; a date prefill that is not a ``YYYY-MM-DD`` string,
or a per-send option list on a non-choice property, is refused loudly before any
network work.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows_components import (
    _chips_single_pick,
    _choice_options_enum,
    _is_array_of_strings,
    _is_choice_field,
    _is_date_field,
    _valid_iso_date,
    component_names,
)
from tai42_channel_whatsapp.flows_resolve import _form_range_merges, _resolve_pages


def build_flow_data(
    schema: dict[str, Any],
    values: dict[str, Any] | None,
    options: dict[str, list[dict[str, Any]]] | None,
    pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``flow_action_payload.data`` a per-send form carries.

    Every field's ``init`` (its prefilled value, or the empty default) and every choice field's ``ds`` (its
    per-send option list ``{id, title}``, or the schema ``enum`` as the default) — keyed by the field's
    identifier-safe component name (:func:`component_names`), matching the published Flow's data keys.

    A choice field is a string property that carries a schema ``enum`` OR one the send
    marks option-bearing (a key in ``options``), OR an array of strings (a CheckboxGroup) —
    matching the published Flow's dynamic choice controls. A per-send option list keyed on a
    property that is neither a string nor an array of strings cannot be honored and is refused
    loudly, naming the field. A prefill on a ``format: date`` field that is not a valid
    ``YYYY-MM-DD`` string is refused loudly too — that is the only shape Meta's date picker
    accepts — before any network work. An array field's ``init`` is the list of selected
    values (``[]`` when unfilled); a boolean's a bool; a single-pick ``chips`` string's a
    one-element (or empty) list; a merged range START's the object ``{start-date, end-date}``
    of both ends' prefills (a merged END carries no ``init`` of its own); every other field's a
    string. ``pages`` (the same step layout the Flow was built with) decides which ``rangeStart``
    pairs merge, so the data agrees with the published controls. Raises ``ChannelInputError``.
    """
    values = values or {}
    options = options or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ChannelInputError("form schema must carry a non-empty 'properties' object")
    names = component_names(properties)

    for name in options:
        prop = properties.get(name)
        if not isinstance(prop, dict) or not (prop.get("type") == "string" or _is_array_of_strings(prop)):
            raise ChannelInputError(
                f"form field {name!r}: WhatsApp cannot apply per-send options to this property "
                "— only a string or an array of strings maps to a choice control"
            )

    resolved_pages, fields_by_screen = _resolve_pages(properties, pages)
    range_merges = _form_range_merges(properties, resolved_pages, fields_by_screen)
    merged_ends = set(range_merges)
    merged_starts = {start: end for end, start in range_merges.items()}

    option_fields = set(options)
    data: dict[str, Any] = {}
    for name, prop in properties.items():
        if name in merged_ends:
            # The merged END carries no ``init``/``ds`` — its START holds the pair's object value.
            continue
        cname = names[name]
        if name in merged_starts:
            data[f"{cname}__init"] = _range_init_value(name, merged_starts[name], values)
            continue
        data[f"{cname}__init"] = _field_init_value(name, prop, values)
        if isinstance(prop, dict) and _is_choice_field(name, prop, option_fields):
            data[f"{cname}__ds"] = _choice_data_source(name, prop, options)
    return data


def _field_init_value(name: str, prop: dict[str, Any], values: dict[str, Any]) -> Any:
    """A non-range field's ``init`` value for the per-send data model.

    The prefilled value shaped for its control (:func:`_init_value`; a single-pick ``chips`` string
    is wrapped in a one-element list), or the control's empty default (``[]`` for an array or a
    single-pick chips, ``False`` for a boolean, ``""`` otherwise). A ``format: date`` prefill that is
    not a ``YYYY-MM-DD`` string is refused loudly — the only shape Meta's date picker accepts.
    """
    prop_type = prop.get("type") if isinstance(prop, dict) else None
    if name in values:
        raw = values[name]
        if _is_date_field(prop) and not (isinstance(raw, str) and _valid_iso_date(raw)):
            raise ChannelInputError(
                f"form property {name!r}: a date field prefill must be a 'YYYY-MM-DD' string "
                f"(the shape Meta's date picker sets and returns), got {raw!r}"
            )
        if _chips_single_pick(prop):
            return [raw if isinstance(raw, str) else str(raw)]
        return _init_value(prop_type, raw)
    if _chips_single_pick(prop):
        return []
    return [] if prop_type == "array" else False if prop_type == "boolean" else ""


def _range_init_value(start: str, end: str, values: dict[str, Any]) -> dict[str, str]:
    """The merged range control's ``init`` — the object of both ends' date prefills (empty strings when unfilled).

    Each end's prefill, when present, must already be a ``YYYY-MM-DD`` string (every date field's
    prefill is proven so by its own date check); an absent prefill is the empty string Meta's range
    control reads as unset.
    """

    def _date_member(name: str) -> str:
        raw = values.get(name)
        if raw is None:
            return ""
        if not (isinstance(raw, str) and _valid_iso_date(raw)):
            raise ChannelInputError(
                f"form property {name!r}: a date field prefill must be a 'YYYY-MM-DD' string "
                f"(the shape Meta's date picker sets and returns), got {raw!r}"
            )
        return raw

    return {"start-date": _date_member(start), "end-date": _date_member(end)}


def _init_value(prop_type: Any, raw: Any) -> Any:
    """A prefilled value shaped for its control's ``init-value``.

    A list for an array (CheckboxGroup), a bool for an OptIn, else a string.
    """
    if prop_type == "boolean":
        return bool(raw)
    if prop_type == "array":
        return raw if isinstance(raw, list) else [str(raw)]
    return raw if isinstance(raw, str) else str(raw)


def _choice_data_source(
    name: str, prop: dict[str, Any], options: dict[str, list[dict[str, Any]]]
) -> list[dict[str, str]]:
    """A choice field's per-send ``data-source`` — the explicit per-send option list, else the schema enum.

    Raises ``ChannelInputError`` when neither is available (a choice control with no options to show).
    """
    if name in options:
        return [{"id": choice["value"], "title": choice.get("label") or choice["value"]} for choice in options[name]]
    enum = _choice_options_enum(prop)
    if enum is None:
        raise ChannelInputError(
            f"form property {name!r}: a choice control needs an enum or per-send options to populate its data-source"
        )
    return [{"id": str(item), "title": str(item)} for item in enum]
