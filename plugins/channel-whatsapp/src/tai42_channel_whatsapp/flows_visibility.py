"""Client-side ``visibleWhen`` rendering for a WhatsApp Flow form.

A field carrying a ``visibleWhen`` predicate renders under Meta's client-side
``If`` components. This module turns a screen's fields into their render VARIANTS
— the component-name suffix and the ordered ``If`` conditions each needs — wraps a
control in those ``If`` nodes, and builds the plain ``${form.…}`` references that carry
each control's answer. A field shown for several values of a controller is drawn as one
control per case; its cases ride the ``data_exchange`` payload one reference each, and the
data endpoint keeps the shown case's value under the field's own name
(:func:`split_answer_cases`). The screen assembly in :mod:`tai42_channel_whatsapp.flows`
consumes these builders.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from typing import Any

from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows_components import _chips_single_pick


def _cond_literal(value: Any) -> str:
    """One ``visibleWhen`` comparison value as a Flow ``If`` expression literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


# Meta's ``If`` components nest at most three deep, so a ``visibleWhen`` dependency chain
# (a field gated on a field that is itself gated, …) renders at most this many nested ``If``.
_MAX_IF_NESTING = 3

# One render VARIANT of a field: the component-name suffix it carries and the ordered list of
# ``If`` conditions that must all hold for it to show. A field with a single-valued predicate has
# one variant with an empty suffix (byte-identical to a plain ``If``); a field shown for several
# values of a controller has one variant per value, each under a distinct suffixed name.
_Variant = tuple[str, list[str]]

# One ``If`` condition of a variant: its Flow expression and the single-valued ``visibleWhen``
# predicate (``{"field", "equals"}`` or ``{"field", "notEmpty"}``) that expression checks.
_Condition = tuple[str, dict[str, Any]]


def _controller_ref(
    field: str, suffix: str, this_fields: list[str], earlier_fields: list[str], names: dict[str, str]
) -> str | None:
    """The ``${…}`` reference to a controlling field's value, or ``None`` when it is unreadable here.

    A controller rendered on this screen is read live as ``${form.<name><suffix>}`` (the suffix
    names the controller's own matching variant); one collected on an earlier screen is read from
    its carrier ``${data.<name>__val}`` (a single value, so no suffix). ``None`` means the
    controller is on neither — the dependent cannot be gated here and renders unconditionally (the
    answer facet still enforces its visibility).
    """
    if field in this_fields:
        return f"${{form.{names[field]}{suffix}}}"
    if field in earlier_fields:
        return f"${{data.{names[field]}__val}}"
    return None


def _screen_cases(
    this_fields: list[str], earlier_fields: list[str], properties: dict[str, Any], names: dict[str, str]
) -> dict[str, list[tuple[str, list[_Condition]]]]:
    """Every field's render variants on a screen, each condition with the predicate it checks.

    The one derivation behind :func:`screen_field_variants` (the drawn ``If`` expressions) and
    :func:`split_answer_cases` (the predicates the data endpoint evaluates to pick the shown case).
    """
    memo: dict[str, list[tuple[str, list[_Condition]]]] = {}

    def resolve(name: str) -> list[tuple[str, list[_Condition]]]:
        if name in memo:
            return memo[name]
        predicate = properties[name].get("visibleWhen")
        controller = predicate.get("field") if isinstance(predicate, dict) else None
        if not isinstance(predicate, dict) or not isinstance(controller, str):
            memo[name] = [("", [])]
            return memo[name]
        controller_variants = resolve(controller) if controller in this_fields else [("", [])]
        if "in" in predicate:
            values = list(predicate["in"])
            if not values:
                raise ChannelInputError(f"form property {name!r}: visibleWhen 'in' names no value")
            split = len(values) > 1
            tests = [(f"== {_cond_literal(choice)}", {"field": controller, "equals": choice}) for choice in values]
            suffixes = [f"__{_slug_value(choice)}" if split else "" for choice in values]
        elif "equals" in predicate:
            tests = [(f"== {_cond_literal(predicate['equals'])}", {"field": controller, "equals": predicate["equals"]})]
            suffixes = [""]
        else:
            tests = [("!= ''", {"field": controller, "notEmpty": True})]
            suffixes = [""]
        variants: list[tuple[str, list[_Condition]]] = []
        for controller_suffix, controller_conditions in controller_variants:
            ref = _controller_ref(controller, controller_suffix, this_fields, earlier_fields, names)
            if ref is None:
                variants.append((controller_suffix, controller_conditions))
                continue
            for suffix, (comparison, test) in zip(suffixes, tests, strict=True):
                variants.append((controller_suffix + suffix, [*controller_conditions, (f"{ref} {comparison}", test)]))
        drawn = [(suffix, [expression for expression, _ in conditions]) for suffix, conditions in variants]
        _guard_variants(name, drawn)
        _guard_split_value_type(name, properties[name], drawn)
        memo[name] = variants
        return variants

    return {name: resolve(name) for name in this_fields}


def screen_field_variants(
    this_fields: list[str], earlier_fields: list[str], properties: dict[str, Any], names: dict[str, str]
) -> dict[str, list[_Variant]]:
    """The render variants of every field on a screen, keyed by schema property name.

    A field with no ``visibleWhen``, or one whose controller is unreadable here, has the single
    unconditional variant ``("", [])``. A field shown when a controller equals ONE value (``equals``,
    a single-entry ``in``) or is non-empty (``notEmpty``) keeps one variant with an empty suffix and
    one ``If`` condition. A field shown for SEVERAL values (a multi-entry ``in``) splits into one
    variant per value, each with a distinct ``__<value>`` suffix and its own ``== <value>``
    condition — Meta's ``||`` refuses a disjunction of equality comparisons, so the disjunction is
    expressed as sibling ``If`` branches instead. When the controller is itself split, the dependent
    inherits each of the controller's variants (its conditions prepended), so a dependency chain
    renders as nested ``If`` under the matching case.
    """
    cases = _screen_cases(this_fields, earlier_fields, properties, names)
    return {
        name: [(suffix, [expression for expression, _ in conditions]) for suffix, conditions in variants]
        for name, variants in cases.items()
    }


def split_answer_cases(
    this_fields: list[str], earlier_fields: list[str], properties: dict[str, Any], names: dict[str, str]
) -> dict[str, list[tuple[str, list[dict[str, Any]]]]]:
    """The per-case controls of every field on a screen drawn as several, keyed by schema property name.

    Each case is its control's component name and the single-valued ``visibleWhen`` predicates that
    must all hold for it to show — the same conditions its ``If`` nodes draw, outermost first. A
    field drawn as one control (no suffix) is absent.
    """
    cases = _screen_cases(this_fields, earlier_fields, properties, names)
    return {
        name: [(names[name] + suffix, [test for _, test in conditions]) for suffix, conditions in variants]
        for name, variants in cases.items()
        if is_split(variants)
    }


def is_split(variants: Sequence[tuple[str, Any]]) -> bool:
    """Whether a field's variants draw it as several per-case controls (each under a suffixed name)."""
    return any(suffix for suffix, _ in variants)


def form_case_names(properties: dict[str, Any], names: dict[str, str], fields_by_screen: list[list[str]]) -> list[str]:
    """The component name of every per-case control the form draws, across its screens."""
    case_names: list[str] = []
    earlier: list[str] = []
    for this_fields in fields_by_screen:
        cases = split_answer_cases(this_fields, earlier, properties, names)
        case_names.extend(case_name for field_cases in cases.values() for case_name, _ in field_cases)
        earlier = [*earlier, *this_fields]
    return case_names


def form_splits_fields(properties: dict[str, Any], names: dict[str, str], fields_by_screen: list[list[str]]) -> bool:
    """Whether any screen of the form draws a field as several per-case controls."""
    return bool(form_case_names(properties, names, fields_by_screen))


def _slug_value(value: Any) -> str:
    """A ``visibleWhen`` value as an identifier-safe component-name suffix fragment.

    Lower-cased, with every run of non-``[a-z0-9]`` characters collapsed to a single ``_`` and the
    ends trimmed. Raises when a value slugs to the empty string — a name suffix must be a real token.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    if not slug:
        raise ChannelInputError(f"visibleWhen value {value!r} has no identifier-safe characters for a case name")
    return slug


def _guard_variants(name: str, variants: list[_Variant]) -> None:
    """Refuse a variant set Meta would reject: colliding case names, or a chain past the nesting cap."""
    suffixes = [suffix for suffix, _ in variants]
    if len(set(suffixes)) != len(suffixes):
        raise ChannelInputError(
            f"form property {name!r}: visibleWhen 'in' values collide after sanitising to identifier-safe case names"
        )
    for _, conditions in variants:
        if len(conditions) > _MAX_IF_NESTING:
            raise ChannelInputError(
                f"form property {name!r}: visibleWhen dependency chain is {len(conditions)} deep; Meta nests "
                f"at most {_MAX_IF_NESTING} If components"
            )


def _guard_split_value_type(name: str, prop: dict[str, Any], variants: list[_Variant]) -> None:
    """Refuse a ``boolean``, ``array`` or chips field drawn as several per-case controls.

    A multi-value ``visibleWhen`` is supported on string-valued conditional fields only on this
    channel; an OptIn, CheckboxGroup or ChipsSelector shown for several values of its controller is
    refused loudly.
    """
    if len(variants) <= 1:
        return
    prop_type = prop.get("type")
    if prop_type in ("boolean", "array") or _chips_single_pick(prop):
        kind = "chips" if _chips_single_pick(prop) else prop_type
        raise ChannelInputError(
            f"form property {name!r}: a {kind} field shown for several values of its controller is not "
            "renderable on WhatsApp (only string-valued conditional fields support a multi-value visibleWhen here)"
        )


def emit_field_variants(
    component: dict[str, Any], variants: list[_Variant], captions: Sequence[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """A control's render nodes: one per variant, the control (uniquely renamed) under its nested ``If``s.

    An unconditional single variant returns the control itself. A conditional variant wraps the
    control — renamed with the variant suffix when it is one of several — in an ``If`` per condition,
    outermost first, so the controller's own condition encloses the dependent's. The ``captions`` (a
    field's second line on a control that cannot hold ``helper-text``) ride immediately after the
    control inside the SAME wrapper, so they show and hide with the control; their text is identical
    across variants (the control's name is suffixed, a caption is not).
    """
    base_name = component["name"]
    nodes: list[dict[str, Any]] = []
    for suffix, conditions in variants:
        control = component if suffix == "" else {**copy.deepcopy(component), "name": base_name + suffix}
        group = [control, *(copy.deepcopy(caption) for caption in captions)]
        for condition in reversed(conditions):
            group = [{"type": "If", "condition": condition, "then": group}]
        nodes.extend(group)
    return nodes


def form_ref(name: str, variants: list[_Variant], names: dict[str, str]) -> str:
    """The ``${form.<name>}`` reference carrying a field drawn as one control.

    A field drawn as several per-case controls has no one reference to its answer: its cases ride a
    ``data_exchange`` payload one plain reference each (:func:`answer_refs`) and the data endpoint
    reads the shown one, so a form with such a field is always endpoint-driven and never reaches a
    static ``navigate``/``complete`` payload. Raises ``ValueError`` when handed one.
    """
    if is_split(variants):
        raise ValueError(f"form property {name!r} is drawn per case; its answer is read by the data endpoint")
    return f"${{form.{names[name]}}}"


def answer_refs(name: str, variants: list[_Variant], names: dict[str, str]) -> dict[str, str]:
    """The ``data_exchange`` payload entries carrying a field's answer, each one plain ``${form.…}`` reference.

    A field drawn as one control rides under its schema name. A field drawn as several per-case
    controls rides one entry per case, keyed by the case's component name — never one expression over
    several references — and the data endpoint keeps the entry of the case whose conditions hold
    (:func:`split_answer_cases`) under the field's schema name.
    """
    if not is_split(variants):
        return {name: form_ref(name, variants, names)}
    return {f"{names[name]}{suffix}": f"${{form.{names[name]}{suffix}}}" for suffix, _ in variants}
