"""The values a ``data_exchange`` step of an endpoint-driven WhatsApp form carries.

A step request carries the person's values in the shapes the Flow's controls report them: a field
drawn as several per-case controls one entry per case, Flow numbers as strings, a merged range as
one pair object under its START. This module reads them back as the form's answer — each per-case
field from its shown case, every value coerced to its schema type and keyed by its schema name —
and lays a reaction's update values over them.
"""

from __future__ import annotations

from typing import Any

from tai42_kit.interactions import evaluate_visible_when

from tai42_channel_whatsapp.correlation import EndpointForm
from tai42_channel_whatsapp.flows_choices import chips_single_pick
from tai42_channel_whatsapp.flows_components import component_names, split_range_values
from tai42_channel_whatsapp.flows_endpoint import RESERVED_REACTION_KEYS
from tai42_channel_whatsapp.flows_visibility import split_answer_cases
from tai42_channel_whatsapp.inbound.form_geometry import screen_fields
from tai42_channel_whatsapp.inbound.forms import coerce_value


def read_split_answers(form: EndpointForm, screen: str, data: dict[str, Any]) -> dict[str, Any]:
    """The request data with each per-case field's answer read from its shown case.

    A field drawn as several per-case controls rides the payload one entry per case, keyed by the
    case's component name (:func:`~tai42_channel_whatsapp.flows_visibility.answer_refs`). The shown
    case is the one whose visibility predicates all hold for the values the request carries — this
    screen's (a per-case controller's own answer read first) and the earlier screens' — evaluated as
    the platform evaluates ``visibleWhen``; its value is kept under the field's schema name. A field
    none of whose cases holds is hidden and carries no value, whatever its undrawn cases carried.
    Every case entry is dropped, so the rest of the exchange reads the field as one value.
    """
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    fields_by_screen, current = screen_fields(form, screen)
    if current is None:
        return data
    earlier = [field for group in fields_by_screen[:current] for field in group]
    cases = split_answer_cases(fields_by_screen[current], earlier, properties, component_names(properties))
    if not cases:
        return data
    case_names = {case_name for field_cases in cases.values() for case_name, _ in field_cases}
    out = {key: value for key, value in data.items() if key not in case_names}
    _ShownCases(cases, properties, data, out).read_all()
    return out


class _ShownCases:
    """Writes each per-case field's shown-case value into ``out`` under its schema name."""

    def __init__(
        self,
        cases: dict[str, list[tuple[str, list[dict[str, Any]]]]],
        properties: dict[str, Any],
        data: dict[str, Any],
        out: dict[str, Any],
    ) -> None:
        self._cases = cases
        self._properties = properties
        self._data = data
        self._out = out
        self._read: set[str] = set()

    def read_all(self) -> None:
        for name in self._cases:
            self._pick(name)

    def _value(self, name: str) -> Any:
        # A controller's value as the platform's ``visibleWhen`` reads it: coerced to its schema type,
        # a per-case controller's own answer read first; ``None`` when the request carries none.
        if name in self._cases:
            self._pick(name)
        return coerce_value(self._out[name], self._properties.get(name)) if name in self._out else None

    def _pick(self, name: str) -> None:
        if name in self._read:
            return
        self._read.add(name)
        for case_name, predicates in self._cases[name]:
            if all(evaluate_visible_when(p, {p["field"]: self._value(p["field"])}) for p in predicates):
                if case_name in self._data:
                    self._out[name] = self._data[case_name]
                return


def partial_values(data: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """The values filled so far, keyed by schema name and coerced to schema types.

    Only declared properties are read (the markers and the forwarded carriers are not values);
    each value is coerced as the inbound answer path coerces (Flow numbers arrive as strings). A
    non-string field whose value is empty (an unfilled number/boolean-less/array) is genuinely not
    yet filled, so it is omitted — partial values are a subset. An empty text value is kept here
    (a reaction sees it as typed); :func:`filled_values` drops it before the shape check.
    """
    partial: dict[str, Any] = {}
    for key, raw in data.items():
        prop = properties.get(key)
        if key in RESERVED_REACTION_KEYS or not isinstance(prop, dict):
            continue
        coerced = coerce_value(raw, prop)
        ptype = prop.get("type")
        # A single-pick chips string's empty value is a ``[]`` (not ``""``), so treat it like the
        # other non-string unfilled defaults and omit it from the partial subset.
        unset_when_empty = ptype != "string" or chips_single_pick(prop)
        if unset_when_empty and coerced in ("", [], None):
            continue
        partial[key] = coerced
    # A merged range control's object value under the START key splits back onto the two date keys.
    return split_range_values(properties, partial)


def filled_values(values: dict[str, Any]) -> dict[str, Any]:
    """The values actually filled, so none other is shape-checked or sent as the answer.

    An empty text control is unfilled (a required one is Meta's own native check), and so is a
    value a reaction cleared (``None``) or emptied (``""``, ``[]``).
    """
    return {name: value for name, value in values.items() if value not in (None, "", [])}


def answer_with_update(partial: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """The answer a clean step or submit stands on: the filled values with the update's values laid over them."""
    return filled_values({**partial, **(update.get("values") or {})})


def range_pair_values(
    start: str, end: str, properties: dict[str, Any], data: dict[str, Any], update_values: dict[str, Any]
) -> dict[str, Any]:
    """A merged range pair's two members with a reaction's values laid over the pair the request carried.

    The carried pair object (under the START's key) is split back onto the two keys with its empty
    members dropped — the control reports an unpicked end as ``""``, which the range shaper reads
    only as absent. An update member then sets its end, and a ``None`` clears it.
    """
    carried = split_range_values(properties, {start: data[start]}) if start in data else {}
    pair = {name: carried[name] for name in (start, end) if carried.get(name) not in (None, "")}
    pair.update({name: update_values[name] for name in (start, end) if name in update_values})
    return pair
