"""Client-side ``visibleWhen`` rendering for a WhatsApp Flow form.

A field carrying a ``visibleWhen`` predicate renders under Meta's client-side
``If`` components. This module turns a screen's fields into their render VARIANTS
— the component-name suffix and the ordered ``If`` conditions each needs — wraps a
control in those ``If`` nodes, and builds the ``${form.…}`` reference that coalesces
a split field's answer back under its own name. The screen assembly in
:mod:`tai42_channel_whatsapp.flows` consumes these builders.
"""

from __future__ import annotations

import copy
import re
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


def _controller_ref(
    field: str, suffix: str, this_fields: list[str], earlier_fields: list[str], names: dict[str, str]
) -> str | None:
    """The ``${…}`` reference to a controlling field's value, or ``None`` when it is unreadable here.

    A controller rendered on this screen is read live as ``${form.<name><suffix>}`` (the suffix
    names the controller's own matching variant); one collected on an earlier screen is read from
    its coalesced carrier ``${data.<name>__val}`` (a single value, so no suffix). ``None`` means the
    controller is on neither — the dependent cannot be gated here and renders unconditionally (the
    answer facet still enforces its visibility).
    """
    if field in this_fields:
        return f"${{form.{names[field]}{suffix}}}"
    if field in earlier_fields:
        return f"${{data.{names[field]}__val}}"
    return None


def screen_field_variants(
    this_fields: list[str], earlier_fields: list[str], properties: dict[str, Any], names: dict[str, str]
) -> dict[str, list[_Variant]]:
    """The render variants of every field on a screen, keyed by schema property name.

    A field with no ``visibleWhen``, or one whose controller is unreadable here, has the single
    unconditional variant ``("", [])``. A field shown when a controller equals ONE value (``equals``,
    a single-entry ``in``) or is non-empty (``notEmpty``) keeps one variant with an empty suffix and
    one ``If`` condition — byte-identical to the former single-``If`` output. A field shown for
    SEVERAL values (a multi-entry ``in``) splits into one variant per value, each with a distinct
    ``__<value>`` suffix and its own ``== <value>`` condition — Meta's ``||`` refuses a disjunction of
    equality comparisons, so the disjunction is expressed as sibling ``If`` branches instead. When the
    controller is itself split, the dependent inherits each of the controller's variants (its
    conditions prepended), so a dependency chain renders as nested ``If`` under the matching case.
    """
    memo: dict[str, list[_Variant]] = {}

    def resolve(name: str) -> list[_Variant]:
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
            comparisons = [f"== {_cond_literal(choice)}" for choice in values]
            suffixes = [f"__{_slug_value(choice)}" if split else "" for choice in values]
        elif "equals" in predicate:
            comparisons = [f"== {_cond_literal(predicate['equals'])}"]
            suffixes = [""]
        else:
            comparisons = ["!= ''"]
            suffixes = [""]
        variants: list[_Variant] = []
        for controller_suffix, controller_conditions in controller_variants:
            ref = _controller_ref(controller, controller_suffix, this_fields, earlier_fields, names)
            if ref is None:
                variants.append((controller_suffix, controller_conditions))
                continue
            for suffix, comparison in zip(suffixes, comparisons, strict=True):
                variants.append((controller_suffix + suffix, [*controller_conditions, f"{ref} {comparison}"]))
        _guard_variants(name, variants)
        _guard_coalescible(name, properties[name], variants)
        memo[name] = variants
        return variants

    return {name: resolve(name) for name in this_fields}


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


def _guard_coalescible(name: str, prop: dict[str, Any], variants: list[_Variant]) -> None:
    """Refuse a split field whose answer cannot coalesce to one typed value across its cases.

    A field shown for several values renders one control per case and its answer is coalesced by
    :func:`form_ref` as a backtick string concatenation — correct only for a STRING value (at most one
    case shows, so the concatenation is that case's string). A ``boolean`` (OptIn) or ``array``
    (CheckboxGroup) field cannot be coalesced this way: the concatenation is a string where Meta
    expects a boolean/array, and stringifying a list loses it. Such a field is refused loudly rather
    than rendered into Meta-refused or value-losing Flow JSON (only string-valued conditional fields
    support a multi-value ``visibleWhen`` on this channel).
    """
    if len(variants) <= 1:
        return
    prop_type = prop.get("type")
    if prop_type in ("boolean", "array") or _chips_single_pick(prop):
        # A single-pick chips field's value is a one-element LIST (like an array), so its per-case
        # answers cannot coalesce by string concatenation either.
        kind = "chips" if _chips_single_pick(prop) else prop_type
        raise ChannelInputError(
            f"form property {name!r}: a {kind} field shown for several values of its controller is not "
            "renderable on WhatsApp — its per-case answers cannot coalesce to one typed value (only "
            "string-valued conditional fields support a multi-value visibleWhen here)"
        )


def emit_field_variants(component: dict[str, Any], variants: list[_Variant]) -> list[dict[str, Any]]:
    """A control's render nodes: one per variant, the control (uniquely renamed) under its nested ``If``s.

    An unconditional single variant returns the control itself. A conditional variant wraps the
    control — renamed with the variant suffix when it is one of several — in an ``If`` per condition,
    outermost first, so the controller's own condition encloses the dependent's.
    """
    base_name = component["name"]
    nodes: list[dict[str, Any]] = []
    for suffix, conditions in variants:
        node = component if suffix == "" else {**copy.deepcopy(component), "name": base_name + suffix}
        for condition in reversed(conditions):
            node = {"type": "If", "condition": condition, "then": [node]}
        nodes.append(node)
    return nodes


def form_ref(name: str, variants: list[_Variant], names: dict[str, str]) -> str:
    """The ``${form.…}`` reference carrying a field's answer, coalesced across its render variants.

    A single-variant field reads ``${form.<name>}`` as before. A split field (always string-valued —
    :func:`_guard_coalescible` refuses a split boolean/array) reads a backtick concatenation of every
    variant's reference; the variants' render conditions are mutually exclusive, so at most one is ever
    shown and the concatenation equals that one's string value (an unshown control resolves to empty),
    keeping one answer under the field's own name.
    """
    component_name = names[name]
    if len(variants) == 1 and variants[0][0] == "":
        return f"${{form.{component_name}}}"
    references = "".join(f"${{form.{component_name}{suffix}}}" for suffix, _ in variants)
    return f"`{references}`"
