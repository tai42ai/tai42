"""The channel-deliverable form-schema subset.

The ONE definition of the SHARED subset every ``form`` question delivered over a
channel must satisfy.

A channel form is answered on the server-rendered callback page, a flat HTML form
the human fills and submits. Only a schema that renders into such a form is
allowed: root ``{"type": "object"}`` with a non-empty ``properties`` map; every
property a scalar (``string``/``boolean``/``integer``/``number``) OR an array whose
``items`` are strings (a multiple-choice field); ``enum`` only on a ``string``
property (or on an array's string ``items``), a non-empty list of strings;
``format`` only on a ``string`` property, one of ``date``/``time``/``date-time``/``email``
(any other ``format`` value, or a ``format`` on a non-string property, is refused
naming the property); a ``required`` list, when present, naming only declared
properties. ``format`` may ride alongside an ``enum`` — the enum's choice control
is rendered and the format is still enforced when the answer is validated. A
property that is neither a scalar nor an array-of-strings — a nested object, an
array of non-strings, a bare ``anyOf``/``oneOf``/``$ref``, a missing type — cannot
be rendered into a control and is refused, naming the offending property.

A date property (a ``string`` with ``format`` ``date``/``date-time``) MAY carry
platform date-constraint keys: ``minDate``/``maxDate`` (``YYYY-MM-DD`` bounds,
inclusive) and ``unavailableDates`` (a list of ``YYYY-MM-DD`` strings and/or
lowercase weekday names that are excluded). A date RANGE is TWO ordinary date
fields: the END field names its start field in ``rangeStart`` and carries the
span bounds ``minDays``/``maxDays`` (inclusive day counts). Each field's answer
stays the plain date string its type declares; the END's ordering + span is
enforced at the single answer facet from the two submitted values. The build-time
rules of these keys are the contract's one check
(:func:`~tai42_contract.interactions.models.form_dates.check_form_date_rules`): a date
key on a non-date property, ``maxDate`` before ``minDate``, ``rangeStart`` naming an
undeclared or non-date property, ``minDays`` above ``maxDays``, or a span key on a
field that declares no ``rangeStart`` is refused loudly.

A ``rangeStart`` pair IS the platform's date range, and the one definition states
both its data shape and how it draws: a surface with a range control draws the pair
as ONE control labelled by the START's and END's ``title`` (else their names); a
surface without one draws two date fields. The answer is always the two date
strings — nothing is recombined. A pair is drawable as one control only when both
fields sit on the same page, both are ``format: date``, both carry the same
``visibleWhen``, and no OTHER field on the page has a ``visibleWhen`` naming either
end; otherwise it draws as two date fields (a documented degrade). A ``rangeStart``
that names another range's END, or two ENDs naming the same start, make the pair
ambiguous and are refused.

Any property MAY carry a ``visibleWhen`` predicate (``{"field": <other property>,
"equals"|"in"|"notEmpty": ...}``) making its visibility depend on another field.
It is evaluated on the submitted values at the answer facet: a field hidden by its
predicate is removed from the answer (not required, not validated) and a value sent
for it is dropped, never an error. The named ``field`` must be another declared
property.

An extra constraint keyword riding alongside a scalar ``type`` (``pattern``,
``minimum``, an inline ``anyOf``, ...) is enforced when the answer is validated, and
each surface catches the shapes it can inside the form before the submit leaves it
(``tai42_skeleton.interactions.form_shapes`` is the one check every surface and the
answer door share).

A ``string`` property MAY carry an ``inputMode`` keyword naming the keyboard/input
kind a channel draws for it (``text``/``numeric``/``decimal``/``tel``/``email``/
``url``). It is a rendering hint only: the value stays the string typed (``numeric``
keeps ``"007"``) and the answer is still validated against ``type``/``pattern``/
``format`` alone. Each channel draws the kind's control where it has one and degrades
to a plain text field by its documented rule where it does not. ``inputMode`` on a
non-string property, an unknown value, or ``inputMode`` alongside an ``enum`` or a
``format`` (which already select the control) — or on an array's ``items`` — is
refused naming the property.

A ``string`` or array-of-strings property MAY carry a ``control`` keyword naming the
CONTROL a surface draws in place of its shape's default: ``chips`` draws a choice (a
string ``enum``, or an array whose string ``items`` declare an ``enum``) as a chip
grid; ``textarea`` draws a ``string`` as a multi-line text control. It changes what
is DRAWN, never what is accepted — the value and its validation stay the property's.
Each channel draws the control where it has one and degrades to the shape's default
control by its documented rule where it does not. ``control`` outside the set
(``chips``/``textarea``), ``chips`` on a non-choice or a choice with no declared
list, ``textarea`` on a non-string or alongside an ``enum``/``format``/``inputMode``
(which already select the control), or ``control`` on an array's ``items`` — is
refused naming the property. ``maxLength``/``minLength`` are drawn where the control
can show them and enforced at the answer door; a ``minLength`` above ``maxLength`` is
a constraint no answer could meet and is refused.

The ask-time guard (``ask``) and the callback form renderer share this ONE
walk, so the shared subset is judged by a single rule set. Channel-SPECIFIC limits
beyond this subset (reserved property names, a medium's own Block Kit / Flow caps)
are NOT defined here — the named channel's ``validate_form_schema`` hook enforces
them at ask-time on top of this walk.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, TypeGuard

import jsonschema
from tai42_contract.channels import Channel
from tai42_contract.interactions.models import (
    FormControl,
    FormInputMode,
    check_form_date_rules,
    check_form_property_names,
)
from tai42_kit.interactions.form_vocabulary import TIME_OF_KEY, VISIBLE_WHEN_OPS, drawn_number_bounds
from tai42_kit.utils.data.json_schema_util import check_plain_json, check_schema_rules

_SCALAR_TYPES = ("string", "boolean", "integer", "number")
# The keyboard/input kinds a string field may declare via ``inputMode`` — the shared
# vocabulary every channel maps to its own control (or a documented plain-text degrade).
_INPUT_MODES = tuple(mode.value for mode in FormInputMode)
# The controls a property may request via ``control`` — the shared set every channel maps to
# its own control (or a documented degrade to the default control for the property's shape).
_FORM_CONTROLS = tuple(control.value for control in FormControl)
# The string formats a channel form may declare, each with a value shape the answer
# door validates (``date`` -> YYYY-MM-DD, ``time`` -> HH:MM[:SS], ``date-time`` -> RFC 3339,
# ``email`` -> the platform's one address shape).
_STRING_FORMATS = ("date", "time", "date-time", "email")


def _validate_array_items(name: str, prop: dict[str, Any]) -> None:
    # An array property renders a multiple-choice control only when its ``items`` are
    # strings (optionally an ``enum`` of strings); any other item schema carries no
    # control and is refused naming the property.
    items = prop.get("items")
    if not isinstance(items, dict):
        raise ValueError(f"form schema property {name!r} is an array but declares no object 'items' schema")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    if items.get("type") != "string":
        raise ValueError(
            f"form schema property {name!r} is an array whose items are not strings; a channel form allows "
            f"only an array of strings (a multiple-choice field)"
        )
    if "inputMode" in items:
        raise ValueError(
            f"form schema property {name!r} items carry inputMode; a multiple-choice field draws no keyboard"
        )
    if "control" in items:
        raise ValueError(f"form schema property {name!r} items carry control; a control is declared on the property")
    enum = items.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise ValueError(f"form schema property {name!r} items enum must be a non-empty list")
        if not all(isinstance(choice, str) for choice in enum):
            raise ValueError(f"form schema property {name!r} items enum must contain only strings")


def _validate_scalar_string(name: str, prop: dict[str, Any], ptype: Any) -> None:
    # ``format``/``enum`` rules for a scalar property. ``format`` only on a string, one of
    # the allowed set; ``enum`` only on a string, a non-empty list of strings.
    fmt = prop.get("format")
    if fmt is not None:
        if ptype != "string":
            raise ValueError(f"form schema property {name!r} has format {fmt!r} but is not a 'string' property")
        if fmt not in _STRING_FORMATS:
            raise ValueError(
                f"form schema property {name!r} has unsupported format {fmt!r}; a channel form allows "
                f"only {', '.join(_STRING_FORMATS)}"
            )
    enum = prop.get("enum")
    if enum is None:
        return
    if ptype != "string":
        raise ValueError(f"form schema property {name!r} has an enum but is not a 'string' property")
    if not isinstance(enum, list) or not enum:
        raise ValueError(f"form schema property {name!r} enum must be a non-empty list")
    if not all(isinstance(choice, str) for choice in enum):
        raise ValueError(f"form schema property {name!r} enum must contain only strings")


def _validate_input_mode(name: str, prop: dict[str, Any], ptype: Any) -> None:
    # The optional ``inputMode`` keyword names the keyboard/input kind a channel draws for a
    # ``string`` field (one of ``_INPUT_MODES``). It is a rendering hint, never a validator —
    # refused loudly where no control could draw it: on a non-string property, for a value
    # outside the set (including a non-string value), or alongside an ``enum`` or a ``format``
    # that already selects the control. Absent means a plain text field.
    mode = prop.get("inputMode")
    if mode is None:
        return
    if ptype != "string":
        raise ValueError(f"form schema property {name!r} has inputMode {mode!r} but is not a 'string' property")
    if mode not in _INPUT_MODES:
        raise ValueError(
            f"form schema property {name!r} has unsupported inputMode {mode!r}; a channel form allows "
            f"only {', '.join(_INPUT_MODES)}"
        )
    if prop.get("enum") is not None:
        raise ValueError(
            f"form schema property {name!r} carries inputMode alongside an enum; a choice control draws no keyboard"
        )
    if prop.get("format") is not None:
        raise ValueError(
            f"form schema property {name!r} carries inputMode alongside format {prop['format']!r}; a format "
            f"already selects the control"
        )


def _validate_chips_control(name: str, prop: dict[str, Any], ptype: Any) -> None:
    # ``control: chips`` draws a choice as a chip grid, so it needs a choice to draw: a
    # ``string`` (or an array of string ``items``) that declares an ``enum`` (a per-send
    # option list may replace it). Any other shape, or a choice with no declared list, is
    # refused naming the property.
    items = prop.get("items")
    is_string = ptype == "string"
    is_string_array = ptype == "array" and isinstance(items, dict) and items.get("type") == "string"
    if not (is_string or is_string_array):
        raise ValueError(
            f"form schema property {name!r} has control 'chips' but is not a string or an array of strings"
        )
    # The choice list lives on the property itself for a string, on its ``items`` for an array.
    enum_source = prop if is_string else (items if isinstance(items, dict) else {})
    if enum_source.get("enum") is None:
        raise ValueError(
            f"form schema property {name!r} has control 'chips' but declares no enum; a chip grid needs a "
            f"declared choice list (a per-send option list may replace it)"
        )


def _validate_textarea_control(name: str, prop: dict[str, Any], ptype: Any) -> None:
    # ``control: textarea`` draws a ``string`` as a multi-line text control, so it is refused
    # on a non-string or alongside a keyword that already selects the control: an ``enum`` (a
    # choice control), a ``format`` (its own control), or an ``inputMode`` (a text area draws
    # the plain keyboard). Each is refused naming the property.
    if ptype != "string":
        raise ValueError(f"form schema property {name!r} has control 'textarea' but is not a 'string' property")
    if prop.get("enum") is not None:
        raise ValueError(
            f"form schema property {name!r} carries control 'textarea' alongside an enum; a choice control "
            f"draws no text area"
        )
    if prop.get("format") is not None:
        raise ValueError(
            f"form schema property {name!r} carries control 'textarea' alongside format {prop['format']!r}; "
            f"a format already selects the control"
        )
    if prop.get("inputMode") is not None:
        raise ValueError(
            f"form schema property {name!r} carries control 'textarea' alongside inputMode {prop['inputMode']!r}; "
            f"a text area draws the plain keyboard"
        )


def _validate_control(name: str, prop: dict[str, Any], ptype: Any) -> None:
    # The optional ``control`` keyword overrides the default control a surface draws for a
    # property's shape (``chips`` for a choice, ``textarea`` for multi-line text). It changes
    # what is DRAWN, never what is accepted. A value outside the set is refused naming the
    # property; the per-control shape rules are checked below.
    control = prop.get("control")
    if control is None:
        return
    if control not in _FORM_CONTROLS:
        raise ValueError(
            f"form schema property {name!r} has unsupported control {control!r}; a channel form allows "
            f"only {', '.join(_FORM_CONTROLS)}"
        )
    if control == FormControl.CHIPS:
        _validate_chips_control(name, prop, ptype)
    else:
        _validate_textarea_control(name, prop, ptype)


def _validate_length_bounds(name: str, prop: dict[str, Any]) -> None:
    # ``minLength``/``maxLength`` are JSON-Schema string bounds the answer door enforces and the
    # text controls draw. A ``minLength`` above ``maxLength`` is a constraint no answer could
    # meet — refused when the form is built, the same discipline as a maxDate before its minDate.
    minimum = prop.get("minLength")
    maximum = prop.get("maxLength")
    if (
        isinstance(minimum, int)
        and not isinstance(minimum, bool)
        and isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and minimum > maximum
    ):
        raise ValueError(f"form schema property {name!r} has minLength {minimum} above maxLength {maximum}")


def _is_number(value: Any) -> TypeGuard[int | float]:
    # A JSON-Schema numeric bound; a bool is not one.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_count_bounds(name: str, prop: dict[str, Any]) -> None:
    # ``minItems``/``maxItems`` bound how many choices an array answer carries. A ``minItems``
    # above ``maxItems`` is a selection count no answer could meet — refused when the form is built.
    minimum = prop.get("minItems")
    maximum = prop.get("maxItems")
    if _is_number(minimum) and _is_number(maximum) and minimum > maximum:
        raise ValueError(f"form schema property {name!r} has minItems {minimum} above maxItems {maximum}")


_NUMBER_BOUNDS = ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum")


def _validate_number_bounds(name: str, prop: dict[str, Any]) -> None:
    # A number property's ``minimum``/``maximum`` and ``exclusiveMinimum``/``exclusiveMaximum``
    # bounds that no value lies within (for an ``integer``, no whole number) are a field no answer
    # could fill — refused when the form is built, so no channel sends a form no one can submit.
    if prop.get("type") not in ("integer", "number"):
        return
    integer = prop.get("type") == "integer"
    low, high = drawn_number_bounds(prop)
    strict_low = strict_high = False
    above = prop.get("exclusiveMinimum")
    below = prop.get("exclusiveMaximum")
    if _is_number(above):
        lowest = math.floor(above) + 1 if integer else above
        if low is None or lowest > low or (not integer and lowest == low):
            low, strict_low = lowest, not integer
    if _is_number(below):
        highest = math.ceil(below) - 1 if integer else below
        if high is None or highest < high or (not integer and highest == high):
            high, strict_high = highest, not integer
    if low is None or high is None:
        return
    if low > high or (low == high and (strict_low or strict_high)):
        declared = ", ".join(f"{key} {prop[key]}" for key in _NUMBER_BOUNDS if _is_number(prop.get(key)))
        what = "whole number" if integer else "number"
        raise ValueError(f"form schema property {name!r} has bounds no {what} can meet ({declared})")


def _validate_visible_when(name: str, prop: dict[str, Any]) -> None:
    # Shape-check the optional ``visibleWhen`` predicate on a property (its referenced
    # field's existence is cross-checked once all properties are known).
    predicate = prop.get("visibleWhen")
    if predicate is None:
        return
    if not isinstance(predicate, dict):
        raise ValueError(f"form schema property {name!r} visibleWhen must be an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    field = predicate.get("field")
    if not isinstance(field, str) or not field:
        raise ValueError(f"form schema property {name!r} visibleWhen must name a non-empty 'field'")
    extra = set(predicate) - {"field", *VISIBLE_WHEN_OPS}
    if extra:
        raise ValueError(f"form schema property {name!r} visibleWhen has unknown keys {sorted(extra)}")
    ops = [op for op in VISIBLE_WHEN_OPS if op in predicate]
    if len(ops) != 1:
        raise ValueError(
            f"form schema property {name!r} visibleWhen must carry exactly one of {', '.join(VISIBLE_WHEN_OPS)}"
        )
    if "in" in predicate and (not isinstance(predicate["in"], list) or not predicate["in"]):
        raise ValueError(f"form schema property {name!r} visibleWhen 'in' must be a non-empty list")
    if "notEmpty" in predicate and predicate["notEmpty"] is not True:
        raise ValueError(f"form schema property {name!r} visibleWhen 'notEmpty' must be true")


def _validate_description(name: str, prop: dict[str, Any]) -> None:
    # The JSON-Schema ``description`` is the field's optional second line (drawn under its
    # ``title`` label where a surface can). It must be a string when present; a non-string
    # already fails loudly at the answer door's schema meta-check, so refusing it here only
    # moves that failure to send time. A blank/whitespace-only description counts as absent
    # (nothing to draw) — the same rule the renderers apply to a blank ``title`` — and is NOT
    # refused: a pydantic-declared schema emits ``"description": ""`` for an empty field doc.
    description = prop.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"form schema property {name!r} description must be a string when present")


def _validate_answerable_bounds(name: str, prop: dict[str, Any]) -> None:
    # Some answer meets every bound a property's answer must meet: the choice count of an array,
    # the length of a scalar, the value of a number.
    ptype = prop.get("type")
    if ptype == "array":
        _validate_count_bounds(name, prop)
    elif ptype in _SCALAR_TYPES:
        _validate_length_bounds(name, prop)
        _validate_number_bounds(name, prop)


def _validate_property(name: str, prop: Any) -> None:
    if not isinstance(prop, dict):
        raise ValueError(f"form schema property {name!r} must be an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    ptype = prop.get("type")
    _validate_input_mode(name, prop, ptype)
    _validate_control(name, prop, ptype)
    _validate_answerable_bounds(name, prop)
    if ptype == "array":
        _validate_array_items(name, prop)
    elif ptype in _SCALAR_TYPES:
        _validate_scalar_string(name, prop, ptype)
    else:
        raise ValueError(
            f"form schema property {name!r} has type {ptype!r}; a channel form allows only a scalar "
            f"type ({', '.join(_SCALAR_TYPES)}) or an array of strings — a property without one (nested "
            f"object, array of non-strings, bare anyOf/oneOf/$ref, missing type) cannot be rendered into a control"
        )
    _validate_description(name, prop)
    _validate_visible_when(name, prop)


def _check_cross_references(fields: list[tuple[str, dict, bool]]) -> None:
    # Resolve the property references the per-property walk could not: a ``visibleWhen.field``
    # must name another declared property.
    props = {name: prop for name, prop, _ in fields}
    for name, prop, _ in fields:
        predicate = prop.get("visibleWhen")
        if isinstance(predicate, dict):
            field = predicate.get("field")
            if field == name:
                raise ValueError(f"form schema property {name!r} visibleWhen cannot reference itself")
            if field not in props:
                raise ValueError(f"form schema property {name!r} visibleWhen names undeclared property {field!r}")


def channel_form_fields(schema: Any) -> list[tuple[str, dict, bool]]:
    """Validate ``schema`` against the channel-deliverable form subset and return its fields.

    Fields come back as ``(name, prop, is_required)`` in declared order.

    Raises ``ValueError`` naming the offending property (or the violated root rule) on
    any deviation from the subset, on bounds no answer could meet, on a number anywhere in the
    schema that is not finite or a schema holding itself (checked first, before any bound is
    compared or any property walked), and on a schema
    breaking the JSON Schema rules, its references included (:func:`check_form_schema_rules`).
    """
    if not isinstance(schema, dict):
        raise ValueError("form schema must be an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    check_plain_json(schema, subject="form schema")
    if schema.get("type") != "object":
        raise ValueError(f"form schema top-level type must be 'object', got {schema.get('type')!r}")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("form schema must carry a non-empty object 'properties' map")
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError("form schema 'required' must be a list when present")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    undeclared = [name for name in required if name not in properties]
    if undeclared:
        raise ValueError(f"form schema 'required' names undeclared properties: {undeclared}")
    required_set = set(required)
    fields: list[tuple[str, dict, bool]] = []
    for name, prop in properties.items():
        key = str(name)
        _validate_property(key, prop)
        fields.append((key, prop, name in required_set))
    _check_cross_references(fields)
    check_form_schema_rules(schema)
    return fields


def check_form_schema_rules(schema: dict[str, Any]) -> None:
    """Raise ``ValueError`` naming the property when ``schema`` breaks the JSON Schema rules.

    The meta-check (Draft 2020-12, with its format checks: a ``pattern`` must compile) is the
    one the form answer check runs before it judges an answer; no number in the schema may be
    an infinity or a NaN and the schema may not hold itself; and every subschema the answer check
    evaluates holds the rules of the dialect it names, with every ``$ref``/``$dynamicRef``
    resolving inside the schema (no schema is fetched) and no cycle of references that never
    descends into the answer (:func:`~tai42_kit.utils.data.json_schema_util.check_schema_rules`).
    No top-level property may be named ``__proto__``, the one name a form drawn in a browser
    cannot keep (:func:`~tai42_contract.interactions.models.forms.check_form_property_names`), and
    its date keys must make sense
    (:func:`~tai42_contract.interactions.models.form_dates.check_form_date_rules`).
    A form schema passing here is one every answer can be judged against.
    """
    check_form_property_names(schema)
    check_form_date_rules(schema)
    check_schema_rules(schema, jsonschema.Draft202012Validator, subject="form schema")


def validate_form_answer_schema(schema: dict[str, Any]) -> None:
    """Refuse a form schema no answer could ever be accepted against; return ``None`` when one could.

    The schema must hold the JSON Schema rules, its references included, carry no number that is
    not finite and not hold itself (:func:`check_form_schema_rules`), and some answer must meet each property's bounds:
    no ``minimum``/``maximum`` (or their exclusive forms) that no value lies within, no
    ``minLength`` above ``maxLength``, no ``minItems`` above ``maxItems``. Raises ``ValueError``
    naming the property. A form delivered over a channel is held to these through
    :func:`channel_form_fields`; this is the check for a form answered in the in-app inbox, whose
    schema is not held to the channel subset.
    """
    check_form_schema_rules(schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for name, prop in properties.items():
        if isinstance(prop, dict):
            _validate_answerable_bounds(str(name), prop)


def validate_channel_form_schema(schema: dict) -> None:
    """Assert ``schema`` conforms to the channel-deliverable form subset.

    Raises ``ValueError`` (naming the offending property and rule) when ``schema`` falls
    outside the subset; returns ``None`` when it conforms. Callers pass a JSON-schema
    dict — normalize a pydantic model first.
    """
    channel_form_fields(schema)


def check_channel_form(channel_obj: Channel, schema: dict[str, Any], question: str) -> None:
    """Refuse a form ``schema`` the channel ``channel_obj`` cannot draw with ``question``.

    The one channel form check every door that draws a form on a channel runs: the shared
    subset walk (:func:`validate_channel_form_schema`), then the channel's OPTIONAL
    ``validate_form_schema(schema, question)`` hook, which enforces the medium's own limits
    (reserved names, per-medium caps, question-text caps). The hook is not called when the walk
    refuses. Raises the walk's or the hook's ``ValueError`` unchanged.
    """
    validate_channel_form_schema(schema)
    validate_form_schema = getattr(channel_obj, "validate_form_schema", None)
    if validate_form_schema is not None:
        validate_form_schema(schema, question)


def paired_time(date_name: str, properties: Mapping[str, Any]) -> str | None:
    """The name of the ``timeOf`` time paired with the date ``date_name`` among ``properties``, else ``None``.

    The build rules admit at most one time per date, so the first match is the one.
    """
    for name, prop in properties.items():
        if isinstance(prop, Mapping) and prop.get(TIME_OF_KEY) == date_name and str(name) != date_name:
            return str(name)
    return None
