"""Answer-schema property → WhatsApp Flow component mapping.

The per-property half of the Flow builder: it maps one answer-schema property (or
one page display block) onto the pieces of Flow JSON that render it — the
identifier-safe component names and human-readable labels, the control classification
(choice / date / text / opt-in), the field control itself, its screen-``data``
declarations and forward-carriers, the display components, and the client-side
visibility wrapper. A field's optional second line (its schema ``description``) draws as the
control's ``helper-text`` where Meta allows it (text and date controls) and as a sibling
``TextCaption`` otherwise (a choice control / opt-in); an option's optional second line (its
``description``) draws under the option title in the data-source item. The screen/footer assembly
and the public ``build_form_flow`` / ``build_flow_data`` entry points consume these builders from
:mod:`tai42_channel_whatsapp.flows`.
"""

from __future__ import annotations

import copy
import datetime
import re
from typing import Any

from tai42_contract.channels import ChannelInputError

# Meta injects ``flow_token`` into every Flow response to correlate the reply, so a
# form property of that name is unanswerable on this channel: the reply handler
# strips the key before the answer reaches the door. The mapper refuses it up front.
_RESERVED_PROPERTY = "flow_token"

# Meta's grammar for a Flow component ``name`` / screen-``data`` key / ``${data.…}``
# reference: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Meta's ``DatePicker`` (Flow JSON ≥ 5.0) sets and returns a ``YYYY-MM-DD`` string. A date
# field's prefill must be exactly that shape, so the emitted ``init-value`` is a value the
# control accepts. The regex fixes the hyphenated calendar-date form; the ``date.fromisoformat``
# check then rejects an impossible date (e.g. ``2026-13-40``).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: The option-count threshold a choice field uses to choose a RadioButtonsGroup over a
#: Dropdown — an ``enum`` of at most this many choices renders as a radio group, more as a
#: dropdown. Mirrors the kit's single documented constant so every surface draws the same
#: short list the same way (the plugin sits below the skeleton and cannot import it).
RADIO_OPTION_THRESHOLD = 5

# The platform date-constraint keys a date property may carry (mirrors the kit's
# ``form_schema`` keys): bounds, excluded days, and a two-field range pairing. Their presence
# makes a date field render as a bounded CalendarPicker rather than a bare DatePicker.
_DATE_CONSTRAINT_KEYS = ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")

# The lowercase weekday names an ``unavailableDates`` entry may carry, in week order, and
# their mapping onto Meta's CalendarPicker ``include-days`` enum codes. A weekday excluded by
# ``unavailableDates`` is dropped from ``include-days`` (the days that STAY selectable).
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_CODES = {
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
    "friday": "Fri",
    "saturday": "Sat",
    "sunday": "Sun",
}


def _is_date_field(prop: Any) -> bool:
    """Whether a property renders as a ``DatePicker`` — a ``string`` carrying ``"format": "date"``.

    ``format: time`` and ``format: date-time`` are not this: the vendor has no time-of-day
    picker, so they stay a ``TextInput``.
    """
    return isinstance(prop, dict) and prop.get("type") == "string" and prop.get("format") == "date"


def _valid_iso_date(value: str) -> bool:
    """Whether ``value`` is a ``YYYY-MM-DD`` calendar date the ``DatePicker`` accepts."""
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _sanitize_name(key: str) -> str:
    """A property name reduced to Meta's identifier grammar.

    Every character outside ``[A-Za-z0-9_]`` becomes ``_``; a result that is empty or
    starts with a digit is prefixed ``f_`` so it opens with a letter or underscore.
    """
    reduced = re.sub(r"[^A-Za-z0-9_]", "_", key)
    if not reduced or reduced[0].isdigit():
        reduced = f"f_{reduced}"
    return reduced


def component_names(properties: dict[str, Any]) -> dict[str, str]:
    """Map each schema property name to a unique identifier-safe Flow component name.

    Meta accepts a component ``name``, a data key and a ``${data.…}`` reference only in
    the grammar ``[A-Za-z_][A-Za-z0-9_]*``, while a form's answer schema may name a
    property with any character. A key already in the grammar is kept verbatim; any
    other is sanitised by :func:`_sanitize_name`. A collision — two keys reducing to the
    same name, a sanitised name equal to a later verbatim key, or a name equal to the
    reserved ``flow_token`` — is disambiguated deterministically in schema order by
    appending ``_2``, ``_3`` … so the first key to claim a name keeps it. Pure and
    order-preserving: the same property list always yields the same map, so a re-send
    and the inbound decode recompute an identical map from the same schema.
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for key in properties:
        base = key if _IDENTIFIER_RE.match(key) else _sanitize_name(key)
        candidate = base
        suffix = 2
        while candidate in used or candidate == _RESERVED_PROPERTY:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        mapping[key] = candidate
    return mapping


def _field_label(name: str, prop: Any) -> str:
    """The human-facing label for a field: its schema ``title`` when a non-empty string, else the property name.

    The one definition both the rendered control label and the completion-payload key
    (:func:`payload_labels`) use, so the key the participant sees in the submitted-form
    summary is always exactly the label shown on the field. A ``title`` that is not a string,
    or is empty or whitespace-only, counts as absent — WhatsApp cannot show a blank field
    label or key — so the property name is used instead.
    """
    title = prop.get("title") if isinstance(prop, dict) else None
    return title if isinstance(title, str) and title.strip() else name


def payload_labels(properties: dict[str, Any]) -> dict[str, str]:
    """Map each schema property name to a unique human-readable completion-payload label.

    The terminal ``complete`` action is keyed by these labels — the label WhatsApp shows on
    each field (its ``title`` when a non-empty string, else the property key; :func:`_field_label`),
    so the submitted-form summary bubble reads in human words. A collision — two properties
    sharing a label, or a label equal to the reserved ``flow_token`` (the correlation token Meta
    injects into every ``nfm_reply``, which the inbound decode strips before the answer reaches
    the door) — is disambiguated deterministically in schema order by appending ``_2``, ``_3`` …
    (the same convention :func:`component_names` uses for names), so the first property to claim a
    label keeps it and no completion key ever clashes with the correlation token. Pure,
    order-preserving and injective: the same property list always yields the same map, so a
    re-send and the inbound decode recompute an identical map from the schema alone and the
    reverse (label → key) is lossless.
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for name, prop in properties.items():
        base = _field_label(name, prop)
        candidate = base
        suffix = 2
        while candidate in used or candidate == _RESERVED_PROPERTY:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        mapping[name] = candidate
    return mapping


def _is_array_of_strings(prop: dict[str, Any]) -> bool:
    """Whether a property is an array whose ``items`` are strings — a multiple-choice field."""
    items = prop.get("items")
    return prop.get("type") == "array" and isinstance(items, dict) and items.get("type") == "string"


def _choice_options_enum(prop: dict[str, Any]) -> list[Any] | None:
    """A choice property's schema option list — a string's ``enum`` or an array's ``items.enum`` — else ``None``."""
    enum = prop["items"].get("enum") if _is_array_of_strings(prop) else prop.get("enum")
    return enum if isinstance(enum, list) else None


def _is_choice_field(name: str, prop: dict[str, Any], option_fields: set[str]) -> bool:
    """Whether a property renders as a dynamic CHOICE control (reads a per-send ``data-source``).

    A string that carries a schema ``enum`` or is marked option-bearing by the ask, OR an
    array of strings (multiple choice). Dropdown, RadioButtonsGroup and CheckboxGroup all read
    their options from the per-send ``${data.<field>__ds}`` list.
    """
    if _is_array_of_strings(prop):
        return True
    if prop.get("type") != "string":
        return False
    return prop.get("enum") is not None or name in option_fields


def _choice_component_type(prop: dict[str, Any]) -> str:
    """The Flow control a choice property renders as.

    An array of strings is a ``CheckboxGroup`` (multiple selection). A string choice with a
    schema ``enum`` of at most :data:`RADIO_OPTION_THRESHOLD` options is a ``RadioButtonsGroup``
    (the short-list control); any other string choice — a longer enum, or an option-bearing
    field whose per-send list length is not fixed at publish — stays the dynamic ``Dropdown``.
    """
    if _is_array_of_strings(prop):
        return "CheckboxGroup"
    enum = prop.get("enum")
    if isinstance(enum, list) and len(enum) <= RADIO_OPTION_THRESHOLD:
        return "RadioButtonsGroup"
    return "Dropdown"


def _date_has_constraints(prop: dict[str, Any]) -> bool:
    """Whether a date property carries any platform date constraint (bounds, excluded days, a range pairing)."""
    return any(key in prop for key in _DATE_CONSTRAINT_KEYS)


def _calendar_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A single-mode ``CalendarPicker`` for a constrained date field.

    Bounds (``min-date``/``max-date``), excluded explicit dates (``unavailable-dates``) and
    excluded weekdays (dropped from ``include-days``, the days that stay selectable) are baked
    as literals from the schema (they are fixed per published Flow). The control sets and
    returns a ``YYYY-MM-DD`` string, exactly like the bare DatePicker, so a date answer's shape
    is unchanged. A range pairing's span (``minDays``/``maxDays``) is enforced at the single
    answer facet, not drawn here (two independent date controls), so the submitted answer stays
    the two scalar date fields.
    """
    component: dict[str, Any] = {
        "type": "CalendarPicker",
        "name": cname,
        "label": label,
        "mode": "single",
        "required": required,
        "init-value": init,
    }
    if isinstance(prop.get("minDate"), str):
        component["min-date"] = prop["minDate"]
    if isinstance(prop.get("maxDate"), str):
        component["max-date"] = prop["maxDate"]
    unavailable = prop.get("unavailableDates")
    if isinstance(unavailable, list):
        dates = [entry for entry in unavailable if isinstance(entry, str) and _ISO_DATE_RE.match(entry)]
        excluded = {entry.lower() for entry in unavailable if isinstance(entry, str) and entry.lower() in _WEEKDAYS}
        if dates:
            component["unavailable-dates"] = dates
        if excluded:
            component["include-days"] = [_WEEKDAY_CODES[day] for day in _WEEKDAYS if day not in excluded]
    return component


def _checkbox_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A ``CheckboxGroup`` for a multiple-choice (array-of-strings) field.

    Reads the per-send option list (``data-source``) and prefill (``init-value``, an array).
    ``min/max-selected-items`` come from the modeled ``minItems``/``maxItems`` bounds; a
    required array with no ``minItems`` floor demands at least one pick (``min-selected-items: 1``).
    """
    component: dict[str, Any] = {
        "type": "CheckboxGroup",
        "name": cname,
        "label": label,
        "required": required,
        "data-source": f"${{data.{cname}__ds}}",
        "init-value": init,
    }
    min_items = prop.get("minItems")
    max_items = prop.get("maxItems")
    if isinstance(min_items, int) and not isinstance(min_items, bool):
        component["min-selected-items"] = min_items
    elif required:
        component["min-selected-items"] = 1
    if isinstance(max_items, int) and not isinstance(max_items, bool):
        component["max-selected-items"] = max_items
    return component


def _dynamic_component(
    name: str, prop: dict[str, Any], required: bool, option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """One Flow field component for the dynamic (per-send) form.

    The component ``name`` and its data references are the identifier-safe name ``names``
    maps the schema key to; the label keeps the property ``title`` (else the raw property
    name). Every control reads its ``init-value`` from the screen ``data`` model and a
    choice field reads a dynamic ``data-source``, so the send injects the values/options. An
    array of strings renders a CheckboxGroup, a short string enum a RadioButtonsGroup (else a
    Dropdown), and a date field with platform constraints a bounded CalendarPicker. Raises
    ``ChannelInputError`` naming a property outside the supported subset.
    """
    label = _field_label(name, prop)
    cname = names[name]
    prop_type = prop.get("type")
    init = f"${{data.{cname}__init}}"
    component: dict[str, Any]
    if _is_array_of_strings(prop):
        if _choice_options_enum(prop) is None and name not in option_fields:
            raise ChannelInputError(
                f"form property {name!r}: a multiple-choice (array of strings) field needs an 'items' enum "
                "or per-send options to render as a CheckboxGroup"
            )
        component = _checkbox_component(cname, label, required, prop, init)
    elif _is_choice_field(name, prop, option_fields):
        kind = _choice_component_type(prop)
        component = {
            "type": kind,
            "name": cname,
            "label": label,
            "required": required,
            "data-source": f"${{data.{cname}__ds}}",
            "init-value": init,
        }
    elif _is_date_field(prop):
        if _date_has_constraints(prop):
            component = _calendar_component(cname, label, required, prop, init)
        else:
            # A plain (unconstrained) date stays a bare DatePicker: its reference defines no
            # ``required`` parameter (unlike every other input control) and Meta rejects an unknown
            # component property at publish, so the required flag is deliberately not emitted here.
            component = {"type": "DatePicker", "name": cname, "label": label, "init-value": init}
    elif prop_type == "string":
        component = {"type": "TextInput", "name": cname, "label": label, "required": required, "init-value": init}
    elif prop_type == "boolean":
        component = {"type": "OptIn", "name": cname, "label": label, "required": required, "init-value": init}
    elif prop_type in ("integer", "number"):
        component = {
            "type": "TextInput",
            "name": cname,
            "label": label,
            "required": required,
            "input-type": "number",
            "init-value": init,
        }
    else:
        raise ChannelInputError(
            f"form property {name!r}: unsupported schema type {prop_type!r} — a form field must be "
            "string, string+enum, an array of strings, boolean, integer, or number (no nested objects, "
            "arrays of non-strings, or unions)"
        )
    return _with_helper_text(component, prop)


#: The Flow controls that take a ``helper-text`` (a field's second line drawn under its label):
#: Meta allows it on these text/date inputs, NOT on a Dropdown/RadioButtonsGroup/CheckboxGroup/OptIn.
#: A field's second line on a control outside this set is drawn as a sibling ``TextCaption`` instead
#: (:func:`field_caption`), so its content is never dropped.
_HELPER_TEXT_TYPES = frozenset({"TextInput", "DatePicker", "CalendarPicker"})


def _field_second_line(prop: dict[str, Any]) -> str | None:
    """A field's optional second line — its schema ``description`` when a non-blank string, else None.

    A blank/whitespace-only description counts as absent (nothing to draw), the same rule
    :func:`_field_label` applies to a blank ``title``.
    """
    description = prop.get("description")
    return description if isinstance(description, str) and description.strip() else None


def _with_helper_text(component: dict[str, Any], prop: dict[str, Any]) -> dict[str, Any]:
    """Add the field's second line as ``helper-text`` when the control supports it.

    A control outside :data:`_HELPER_TEXT_TYPES` (a choice control or an OptIn) keeps no
    helper-text; its second line is drawn as a sibling :func:`field_caption` so it is never lost.
    """
    description = _field_second_line(prop)
    if description is not None and component["type"] in _HELPER_TEXT_TYPES:
        component["helper-text"] = description
    return component


def field_caption(component: dict[str, Any], prop: dict[str, Any]) -> dict[str, Any] | None:
    """A ``TextCaption`` node carrying a field's second line for a control that cannot hold ``helper-text``.

    Returns None when the field has no second line or its control already drew it as ``helper-text``
    (:data:`_HELPER_TEXT_TYPES`). The caption is emitted immediately after the control and, for a
    conditional field, inside the same ``If`` wrapper so it shows and hides with the control.
    """
    description = _field_second_line(prop)
    if description is None or component["type"] in _HELPER_TEXT_TYPES:
        return None
    return {"type": "TextCaption", "text": description}


_DS_ITEM_EXAMPLE = [{"id": "a", "title": "a", "description": "a"}]


def data_source_item(choice: dict[str, Any]) -> dict[str, str]:
    """One ``data-source`` row for a per-send option (shared by the publish and reaction paths).

    Carries the option's ``id``/``title`` and, when the option declares a non-blank second line,
    its ``description`` (drawn under the option title on a choice control). The ``description`` key
    is omitted when absent, so an option without a second line renders exactly as before.
    """
    item: dict[str, str] = {"id": choice["value"], "title": choice.get("label") or choice["value"]}
    description = choice.get("description")
    if isinstance(description, str) and description.strip():
        item["description"] = description
    return item


def _value_type_decl(prop: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` type declaration for a field's value.

    Boolean for an OptIn, array for a CheckboxGroup, else a string.
    Used both for a field's ``init-value`` source and for a value collected on an earlier
    screen and forwarded to completion, so the declared type always matches the control.
    """
    prop_type = prop.get("type")
    if prop_type == "boolean":
        return {"type": "boolean", "__example__": False}
    if prop_type == "array":
        return {"type": "array", "items": {"type": "string"}, "__example__": []}
    return {"type": "string", "__example__": ""}


def _field_data_decls(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """The screen-``data`` declarations a field needs where it is RENDERED.

    Its ``init-value`` source and, for a choice field, its dynamic ``data-source`` —
    keyed by the field's identifier-safe component name.
    """
    cname = names[name]
    decls: dict[str, Any] = {f"{cname}__init": _value_type_decl(prop)}
    if _is_choice_field(name, prop, option_fields):
        decls[f"{cname}__ds"] = {
            "type": "array",
            "items": {
                "type": "object",
                # ``description`` (an option's optional second line) is ALWAYS declared on the item,
                # whether or not a given send carries one, so the published Flow shape — and so its
                # cache key — does not depend on a per-send option list having descriptions.
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
            "__example__": _DS_ITEM_EXAMPLE,
        }
    return decls


def _forward_field_data(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> dict[str, str]:
    """The navigate-payload entries that carry a downstream field's ``init``/``ds`` on to the next screen.

    They enter only at the entry screen, so each step re-forwards the ones its successors
    still need — keyed by the field's identifier-safe component name.
    """
    cname = names[name]
    forwarded = {f"{cname}__init": f"${{data.{cname}__init}}"}
    if _is_choice_field(name, prop, option_fields):
        forwarded[f"{cname}__ds"] = f"${{data.{cname}__ds}}"
    return forwarded


def _display_component(block: dict[str, Any], slots: dict[str, str]) -> dict[str, Any]:
    """One Flow display component (TextHeading / TextBody / Image) for a page display block.

    A static block carries its own ``text``/``src``; a slotted block reads ``${data.<slot>}``
    (filled by a reaction while the form is open, empty until then). An image maps to the vendor
    ``Image`` (base64 ``src`` + ``alt-text``), a heading to ``TextHeading``, a body to ``TextBody``.
    """
    kind = block.get("kind")
    slot = block.get("slot")
    if kind == "image":
        src = f"${{data.{slots[slot]}}}" if isinstance(slot, str) else block.get("src")
        component: dict[str, Any] = {"type": "Image", "src": src}
        if isinstance(block.get("alt"), str):
            component["alt-text"] = block["alt"]
        return component
    text = f"${{data.{slots[slot]}}}" if isinstance(slot, str) else block.get("text")
    return {"type": "TextHeading" if kind == "heading" else "TextBody", "text": text}


def _display_component_list(page: dict[str, Any], slots: dict[str, str]) -> list[dict[str, Any]]:
    """The ordered display components for a page (empty when it carries none)."""
    return [_display_component(block, slots) for block in (page.get("display") or [])]


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
    if prop_type in ("boolean", "array"):
        raise ChannelInputError(
            f"form property {name!r}: a {prop_type} field shown for several values of its controller is not "
            "renderable on WhatsApp — its per-case answers cannot coalesce to one typed value (only "
            "string-valued conditional fields support a multi-value visibleWhen here)"
        )


def emit_field_variants(
    component: dict[str, Any], variants: list[_Variant], caption: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """A control's render nodes: one per variant, the control (uniquely renamed) under its nested ``If``s.

    An unconditional single variant returns the control itself. A conditional variant wraps the
    control — renamed with the variant suffix when it is one of several — in an ``If`` per condition,
    outermost first, so the controller's own condition encloses the dependent's. A ``caption`` (a
    field's second line on a control that cannot hold ``helper-text``) rides immediately after the
    control inside the SAME wrapper, so it shows and hides with the control; its text is identical
    across variants (the control's name is suffixed, the caption is not).
    """
    base_name = component["name"]
    nodes: list[dict[str, Any]] = []
    for suffix, conditions in variants:
        control = component if suffix == "" else {**copy.deepcopy(component), "name": base_name + suffix}
        group = [control] if caption is None else [control, copy.deepcopy(caption)]
        for condition in reversed(conditions):
            group = [{"type": "If", "condition": condition, "then": group}]
        nodes.extend(group)
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
