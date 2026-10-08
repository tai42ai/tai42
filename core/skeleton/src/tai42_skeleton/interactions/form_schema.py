"""The channel-deliverable form-schema subset.

The ONE definition of the SHARED subset every ``form`` question delivered over a
channel must satisfy.

A channel form is answered on the server-rendered callback page, a flat HTML form
the human fills and submits. Only a schema that renders into such a form is
allowed: root ``{"type": "object"}`` with a non-empty ``properties`` map; every
property a scalar (``string``/``boolean``/``integer``/``number``) OR an array whose
``items`` are strings (a multiple-choice field); ``enum`` only on a ``string``
property (or on an array's string ``items``), a non-empty list of strings;
``format`` only on a ``string`` property, one of ``date``/``time``/``date-time``
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
enforced at the single answer facet from the two submitted values. A date key on a
non-date property, ``maxDate`` before ``minDate``, ``rangeStart`` naming an
undeclared or non-date property, ``minDays`` above ``maxDays``, or a span key on a
field that declares no ``rangeStart`` is refused loudly.

Any property MAY carry a ``visibleWhen`` predicate (``{"field": <other property>,
"equals"|"in"|"notEmpty": ...}``) making its visibility depend on another field.
It is evaluated on the submitted values at the answer facet: a field hidden by its
predicate is removed from the answer (not required, not validated) and a value sent
for it is dropped, never an error. The named ``field`` must be another declared
property.

An extra constraint keyword riding alongside a scalar ``type`` (``pattern``,
``minimum``, an inline ``anyOf``, ...) is not rendered but is enforced when the
answer is validated.

The ask-time guard (``ask``) and the callback form renderer share this ONE
walk, so the shared subset is judged by a single rule set. Channel-SPECIFIC limits
beyond this subset (reserved property names, a medium's own Block Kit / Flow caps)
are NOT defined here — the named channel's ``validate_form_schema`` hook enforces
them at ask-time on top of this walk.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

_SCALAR_TYPES = ("string", "boolean", "integer", "number")
# The string formats a channel form may declare, each with a value shape the answer
# door validates (``date`` -> YYYY-MM-DD, ``time`` -> HH:MM[:SS], ``date-time`` -> RFC 3339).
_STRING_FORMATS = ("date", "time", "date-time")
# The two formats that carry a real calendar date, so date constraints (bounds,
# unavailable days, a range pairing) apply to them.
_DATE_FORMATS = ("date", "date-time")

# The lowercase weekday names an ``unavailableDates`` entry may name to exclude a
# recurring weekday (alongside explicit ``YYYY-MM-DD`` dates).
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# The ``visibleWhen`` operators — exactly one of these rides a predicate.
_VISIBLE_WHEN_OPS = ("equals", "in", "notEmpty")

#: The option-count threshold a renderer uses to choose a radio group over a dropdown/select
#: for a choice field: an ``enum`` (or per-send option list) with AT MOST this many choices
#: renders as a radio group, more as a dropdown. One shared, documented constant so every
#: surface draws the same short list the same way.
RADIO_OPTION_THRESHOLD = 5

_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def _is_iso_date(value: Any) -> bool:
    """A ``YYYY-MM-DD`` (zero-padded) string naming a real calendar date."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _prop_is_date(prop: dict[str, Any]) -> bool:
    """Whether ``prop`` is a date-carrying property (a ``string`` with a date ``format``)."""
    return prop.get("type") == "string" and prop.get("format") in _DATE_FORMATS


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


def _check_bound_date(name: str, key: str, value: Any) -> None:
    if not _is_iso_date(value):
        raise ValueError(f"form schema property {name!r} {key} must be a YYYY-MM-DD date, got {value!r}")


_DATE_CONSTRAINT_KEYS = ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")


def _validate_date_bounds(name: str, prop: dict[str, Any]) -> None:
    if "minDate" in prop:
        _check_bound_date(name, "minDate", prop["minDate"])
    if "maxDate" in prop:
        _check_bound_date(name, "maxDate", prop["maxDate"])
    if "minDate" in prop and "maxDate" in prop and prop["maxDate"] < prop["minDate"]:
        raise ValueError(
            f"form schema property {name!r} has maxDate {prop['maxDate']!r} before minDate {prop['minDate']!r}"
        )
    if "unavailableDates" in prop:
        entries = prop["unavailableDates"]
        if not isinstance(entries, list):
            raise ValueError(f"form schema property {name!r} unavailableDates must be a list")
        for entry in entries:
            if not (isinstance(entry, str) and (_is_iso_date(entry) or entry.lower() in _WEEKDAYS)):
                raise ValueError(
                    f"form schema property {name!r} unavailableDates entries must be a YYYY-MM-DD date or a "
                    f"weekday name ({', '.join(_WEEKDAYS)}), got {entry!r}"
                )


def _validate_range_span(name: str, prop: dict[str, Any]) -> None:
    has_range = "rangeStart" in prop
    if has_range and (not isinstance(prop["rangeStart"], str) or not prop["rangeStart"].strip()):
        raise ValueError(f"form schema property {name!r} rangeStart must name a start date property")
    for span in ("minDays", "maxDays"):
        if span not in prop:
            continue
        if not has_range:
            raise ValueError(f"form schema property {name!r} carries {span} but declares no rangeStart")
        value = prop[span]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"form schema property {name!r} {span} must be a non-negative integer, got {value!r}")
    if "minDays" in prop and "maxDays" in prop and prop["minDays"] > prop["maxDays"]:
        raise ValueError(f"form schema property {name!r} has minDays {prop['minDays']} above maxDays {prop['maxDays']}")


def _validate_date_constraints(name: str, prop: dict[str, Any]) -> None:
    # Shape-check the optional date-constraint keys on a property. Every key requires a
    # date property; the values must be well-formed; a range END field carries a
    # ``rangeStart`` and its span bounds, and a span key without ``rangeStart`` is a bug.
    present = [key for key in _DATE_CONSTRAINT_KEYS if key in prop]
    if present and not _prop_is_date(prop):
        raise ValueError(
            f"form schema property {name!r} carries date constraints {present} but is not a date property "
            f"(a 'string' with format 'date'/'date-time')"
        )
    _validate_date_bounds(name, prop)
    _validate_range_span(name, prop)


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
    extra = set(predicate) - {"field", *_VISIBLE_WHEN_OPS}
    if extra:
        raise ValueError(f"form schema property {name!r} visibleWhen has unknown keys {sorted(extra)}")
    ops = [op for op in _VISIBLE_WHEN_OPS if op in predicate]
    if len(ops) != 1:
        raise ValueError(
            f"form schema property {name!r} visibleWhen must carry exactly one of {', '.join(_VISIBLE_WHEN_OPS)}"
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


def _validate_property(name: str, prop: Any) -> None:
    if not isinstance(prop, dict):
        raise ValueError(f"form schema property {name!r} must be an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    ptype = prop.get("type")
    if ptype == "array":
        _validate_array_items(name, prop)
    elif ptype in _SCALAR_TYPES:
        _validate_scalar_string(name, prop, ptype)
        _validate_date_constraints(name, prop)
    else:
        raise ValueError(
            f"form schema property {name!r} has type {ptype!r}; a channel form allows only a scalar "
            f"type ({', '.join(_SCALAR_TYPES)}) or an array of strings — a property without one (nested "
            f"object, array of non-strings, bare anyOf/oneOf/$ref, missing type) cannot be rendered into a control"
        )
    _validate_description(name, prop)
    _validate_visible_when(name, prop)


def _check_cross_references(fields: list[tuple[str, dict, bool]]) -> None:
    # Resolve the property references the per-property walk could not: a ``rangeStart``
    # must name a declared DATE property (not itself), and a ``visibleWhen.field`` must
    # name another declared property.
    props = {name: prop for name, prop, _ in fields}
    for name, prop, _ in fields:
        start = prop.get("rangeStart")
        if isinstance(start, str) and start:
            if start == name:
                raise ValueError(f"form schema property {name!r} rangeStart cannot name itself")
            target = props.get(start)
            if target is None:
                raise ValueError(f"form schema property {name!r} rangeStart names undeclared property {start!r}")
            if not _prop_is_date(target):
                raise ValueError(f"form schema property {name!r} rangeStart names non-date property {start!r}")
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
    any deviation from the subset.
    """
    if not isinstance(schema, dict):
        raise ValueError("form schema must be an object")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
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
    return fields


def validate_channel_form_schema(schema: dict) -> None:
    """Assert ``schema`` conforms to the channel-deliverable form subset.

    Raises ``ValueError`` (naming the offending property and rule) when ``schema`` falls
    outside the subset; returns ``None`` when it conforms. Callers pass a JSON-schema
    dict — normalize a pydantic model first.
    """
    channel_form_fields(schema)


def evaluate_visible_when(predicate: dict[str, Any], values: dict[str, Any]) -> bool:
    """Whether a property with ``predicate`` is VISIBLE given the submitted ``values``.

    ``equals`` is visible iff the controlling field equals the given value; ``in`` iff its
    value is one of the given list; ``notEmpty`` iff its value is present and not an empty
    string / list / object. An absent controlling value reads as unset (``None``).
    """
    controlling = values.get(predicate["field"])
    if "equals" in predicate:
        return controlling == predicate["equals"]
    if "in" in predicate:
        return controlling in predicate["in"]
    # notEmpty
    return controlling not in (None, "", [], {})


def hidden_fields(schema: dict[str, Any], values: dict[str, Any]) -> set[str]:
    """The declared properties HIDDEN by their ``visibleWhen`` predicate for the submitted ``values``.

    A property with no predicate is always visible. The predicate is evaluated on the
    submitted values once (no cascading re-evaluation): the documented meaning of a
    conditional field.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return set()
    hidden: set[str] = set()
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        predicate = prop.get("visibleWhen")
        if isinstance(predicate, dict) and not evaluate_visible_when(predicate, values):
            hidden.add(str(name))
    return hidden


def _answer_date(value: Any) -> datetime.date | None:
    """The calendar date a submitted date/date-time answer names, or ``None`` when it is not one.

    A ``YYYY-MM-DD`` value yields that date; an RFC 3339 ``date-time`` yields its date part
    (compared against the ``YYYY-MM-DD`` bounds on the date part).
    """
    if not isinstance(value, str):
        return None
    if _is_iso_date(value):
        return datetime.date.fromisoformat(value)
    try:
        return datetime.datetime.fromisoformat(value.upper()).date()
    except ValueError:
        return None


def _date_bounds_mismatch(name: str, prop: dict[str, Any], the_date: datetime.date) -> str | None:
    # The message for a date outside this property's min/max or on an unavailable day, else None.
    minimum = prop.get("minDate")
    if isinstance(minimum, str) and the_date < datetime.date.fromisoformat(minimum):
        return f"date for {name!r} must be on or after {minimum}"
    maximum = prop.get("maxDate")
    if isinstance(maximum, str) and the_date > datetime.date.fromisoformat(maximum):
        return f"date for {name!r} must be on or before {maximum}"
    unavailable = prop.get("unavailableDates")
    if isinstance(unavailable, list):
        weekday = _WEEKDAYS[the_date.weekday()]
        iso = the_date.isoformat()
        for entry in unavailable:
            if isinstance(entry, str) and (entry == iso or entry.lower() == weekday):
                return f"date for {name!r} ({iso}) is unavailable"
    return None


def _range_mismatch(key: str, prop: dict[str, Any], the_date: datetime.date, answer: dict[str, Any]) -> str | None:
    # The message for a range END field whose end precedes its start or whose inclusive
    # day-count falls outside ``minDays``/``maxDays``, else None (no range, or within bounds).
    start_name = prop.get("rangeStart")
    if not (isinstance(start_name, str) and start_name):
        return None
    start_date = _answer_date(answer.get(start_name))
    if start_date is None:
        return None
    if the_date < start_date:
        return f"date range end {key!r} must be on or after its start {start_name!r}"
    span = (the_date - start_date).days + 1
    minimum_days = prop.get("minDays")
    if isinstance(minimum_days, int) and span < minimum_days:
        return f"date range {key!r} spans {span} day(s), at least {minimum_days} required"
    maximum_days = prop.get("maxDays")
    if isinstance(maximum_days, int) and span > maximum_days:
        return f"date range {key!r} spans {span} day(s), at most {maximum_days} allowed"
    return None


def date_constraint_mismatch(schema: dict[str, Any], answer: dict[str, Any]) -> tuple[str, str] | None:
    """Enforce the platform date constraints on a submitted ``answer``; return ``(message, field)`` or ``None``.

    For each date property present in the answer: the value must fall within its
    ``minDate``/``maxDate`` (inclusive) and off its ``unavailableDates``. For a range END
    field (one declaring ``rangeStart``) the end must be on or after its start and the
    inclusive day-count between them within ``minDays``/``maxDays``. The answer is read as
    already type-valid (``jsonschema.validate`` ran first), so a value that is not a date is
    ignored here. ``field`` is the offending date field so a surface can pin the error.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    for name, prop in properties.items():
        if not isinstance(prop, dict) or not _prop_is_date(prop):
            continue
        key = str(name)
        the_date = _answer_date(answer.get(key))
        if the_date is None:
            continue
        message = _date_bounds_mismatch(key, prop, the_date) or _range_mismatch(key, prop, the_date, answer)
        if message is not None:
            return message, key
    return None


def effective_answer_schema(schema: dict[str, Any], data: Any, choices: Any = ()) -> dict[str, Any]:
    """Return ``schema`` shaped for answer validation: per-send option lists applied, reaction-fed choices typed-only.

    A per-send list replaces the published ``enum`` for one send, so a submitted value is
    judged against the choices the human was actually shown — EXCEPT for a field named in
    ``choices`` (a reaction may have replaced its list while the form was open): such a
    field has its ``enum`` OMITTED entirely so only its TYPE is checked, membership being
    the consumer's at the ``submitted`` event. ``data`` is the stored :class:`FormData`
    dump (or ``None``); ``choices`` is the reaction-fed-choice field names. Non-mutating
    (shallow copies only).
    """
    options = (data or {}).get("options") if isinstance(data, dict) else None
    choice_set = {str(name) for name in (choices or ())}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    if not options and not choice_set:
        return schema
    new_properties = dict(properties)
    for name, option_list in (options or {}).items():
        if name in choice_set:
            # A reaction-fed choice: the send-time list is not stamped (the reaction may
            # replace it); membership is checked by the consumer at submit, not here.
            continue
        prop = new_properties.get(name)
        if not isinstance(prop, dict):
            continue
        enum = [option["value"] for option in option_list]
        if prop.get("type") == "array":
            # An array property carries its choices on ``items``, not the array itself:
            # placing the ``enum`` on the array would demand the whole submitted list
            # equal one option, rejecting every real multi-select answer.
            items = prop.get("items")
            items = items if isinstance(items, dict) else {}
            new_properties[name] = {**prop, "items": {**items, "enum": enum}}
        else:
            new_properties[name] = {**prop, "enum": enum}
    for name in choice_set:
        prop = new_properties.get(name)
        if not isinstance(prop, dict):
            continue
        stripped = {key: value for key, value in prop.items() if key != "enum"}
        if stripped.get("type") == "array" and isinstance(stripped.get("items"), dict):
            stripped = {**stripped, "items": {k: v for k, v in stripped["items"].items() if k != "enum"}}
        new_properties[name] = stripped
    return {**schema, "properties": new_properties}
