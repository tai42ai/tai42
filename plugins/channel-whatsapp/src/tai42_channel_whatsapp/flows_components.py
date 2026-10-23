"""Answer-schema property → WhatsApp Flow component mapping.

The per-property half of the Flow builder: it maps one answer-schema property (or
one page display block) onto the pieces of Flow JSON that render it — the
identifier-safe component names and human-readable labels, the control classification
(choice / date / text / opt-in), the field control itself, its screen-``data``
declarations and forward-carriers, the display components, and the client-side
visibility wrapper. The choice classification lives in :mod:`tai42_channel_whatsapp.flows_choices`
and a field's or option's second line in :mod:`tai42_channel_whatsapp.flows_second_line`. The
screen/footer assembly and the public ``build_form_flow`` / ``build_flow_data`` entry points consume these builders from
:mod:`tai42_channel_whatsapp.flows`.
"""

from __future__ import annotations

import copy
import datetime
import re
from typing import Any, assert_never

from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import FormControl, FormInputMode
from tai42_kit.interactions import FORM_WEEKDAYS

from tai42_channel_whatsapp.flows_choices import (
    _CHIPS_MAX_CHOICES,
    _chips_single_pick,
    _choice_component_type,
    _choice_options_enum,
    _draws_chips,
    _is_array_of_strings,
    _is_choice_field,
)
from tai42_channel_whatsapp.flows_second_line import (
    _CHIPS_DS_ITEM_EXAMPLE,
    _CHIPS_DS_ITEM_PROPERTIES,
    _DS_ITEM_EXAMPLE,
    _DS_ITEM_PROPERTIES,
    _HELPER_TEXT_MAX_CHARS,
    _with_helper_text,
)
from tai42_channel_whatsapp.flows_shapes import ERROR_MESSAGE_TYPES, error_binding, helper_word, native_pattern

# Meta injects ``flow_token`` into every Flow response to correlate the reply, so a
# form property of that name is unanswerable on this channel: the reply handler
# strips the key before the answer reaches the door. The mapper refuses it up front.
_RESERVED_PROPERTY = "flow_token"

# Meta's grammar for a Flow component ``name`` / screen-``data`` key / ``${data.…}``
# reference: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Meta's ``CalendarPicker`` (Flow JSON ≥ 6.1) sets and returns a ``YYYY-MM-DD`` string. A date
# field's prefill must be exactly that shape, so the emitted ``init-value`` is a value the
# control accepts. The regex fixes the hyphenated calendar-date form; the ``date.fromisoformat``
# check then rejects an impossible date (e.g. ``2026-13-40``).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The platform's form vocabulary, from ``tai42_kit.interactions``: the weekday names an
# ``unavailableDates`` entry may carry. Each weekday maps onto Meta's CalendarPicker
# ``include-days`` enum code below; an excluded weekday is dropped from ``include-days`` (the
# days that STAY selectable).


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
    """Whether a property renders as a date ``CalendarPicker`` — a ``string`` carrying ``"format": "date"``.

    ``format: time`` and ``format: date-time`` are not this: the vendor has no time-of-day
    picker, so they stay a ``TextInput``.
    """
    return isinstance(prop, dict) and prop.get("type") == "string" and prop.get("format") == "date"


def _valid_iso_date(value: str) -> bool:
    """Whether ``value`` is a ``YYYY-MM-DD`` calendar date the ``CalendarPicker`` accepts."""
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
    (:func:`payload_labels`) use, so each completion key is exactly the label shown on its
    field. A ``title`` that is not a string, or is empty or whitespace-only, counts as absent,
    so the property name is used instead and no label or key is blank.
    """
    title = prop.get("title") if isinstance(prop, dict) else None
    return title if isinstance(title, str) and title.strip() else name


def payload_labels(properties: dict[str, Any]) -> dict[str, str]:
    """Map each schema property name to a unique human-readable completion-payload label.

    The terminal ``complete`` action is keyed by these labels — the label each field's control
    shows (its ``title`` when a non-empty string, else the property key; :func:`_field_label`).
    A collision — two properties
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


def _chips_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A ``ChipsSelector`` for a ``control: chips`` choice field.

    A string enum is SINGLE-pick (``max-selected-items: 1``); its value is a one-element list,
    decoded back to the string. An array of strings is MULTI-pick: ``min/max-selected-items`` come
    from the modeled ``minItems``/``maxItems`` (a required array with no ``minItems`` floor demands
    at least one pick; an absent ``maxItems`` is Meta's 20 ceiling). Both read the per-send option
    list (``data-source``) and prefill (``init-value``, an array).
    """
    component: dict[str, Any] = {
        "type": "ChipsSelector",
        "name": cname,
        "label": label,
        "required": required,
        "data-source": f"${{data.{cname}__ds}}",
        "init-value": init,
    }
    if _is_array_of_strings(prop):
        min_items = prop.get("minItems")
        max_items = prop.get("maxItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool):
            component["min-selected-items"] = min_items
        elif required:
            component["min-selected-items"] = 1
        component["max-selected-items"] = (
            max_items if isinstance(max_items, int) and not isinstance(max_items, bool) else _CHIPS_MAX_CHOICES
        )
    else:
        component["max-selected-items"] = 1
    return component


def _calendar_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A single-mode ``CalendarPicker`` for a date field.

    Every date draws this control: unlike Meta's ``DatePicker`` it carries ``required``, a
    ``helper-text`` and an ``error-message``. Bounds (``min-date``/``max-date``), excluded explicit
    dates (``unavailable-dates``) and excluded weekdays (dropped from ``include-days``, the days
    that stay selectable) are baked as literals from the schema (they are fixed per published
    Flow). The control sets and returns a ``YYYY-MM-DD`` string. A range pairing drawn as two
    controls has its span (``minDays``/``maxDays``) checked at the data endpoint and the answer
    door, so the submitted answer stays the two scalar date fields.
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
        excluded = {entry.lower() for entry in unavailable if isinstance(entry, str) and entry.lower() in FORM_WEEKDAYS}
        if dates:
            component["unavailable-dates"] = dates
        if excluded:
            component["include-days"] = [_WEEKDAY_CODES[day] for day in FORM_WEEKDAYS if day not in excluded]
    return component


def _sibling_depends_on(fields: list[str], properties: dict[str, Any], pair: set[str]) -> bool:
    """Whether any field OTHER than the pair itself has a ``visibleWhen`` naming either end of the pair.

    A controller reference to a merged pair's end would read the one range control's OBJECT value as
    a scalar (same screen) or a carrier the merged-away end never emits (an earlier screen), so a pair
    any other field depends on is NOT merged — it draws as two separate date controls instead.
    """
    for name in fields:
        if name in pair:
            continue
        prop = properties.get(name)
        predicate = prop.get("visibleWhen") if isinstance(prop, dict) else None
        if isinstance(predicate, dict) and predicate.get("field") in pair:
            return True
    return False


def range_pairs(this_fields: list[str], properties: dict[str, Any], has_review_page: bool = False) -> dict[str, str]:
    """The ``END name -> START name`` map for every ``rangeStart`` pair drawable as ONE range control.

    A ``rangeStart`` pair (the END names its START) draws as one :func:`_calendar_range_component`
    when both ends sit on THIS screen, both are ``format: date``, both carry the SAME ``visibleWhen``
    (both absent or deep-equal) and it is not shown for several values, and no OTHER field anywhere
    in the form has a ``visibleWhen`` naming either end (:func:`_sibling_depends_on`, checked over
    all ``properties`` so a dependant on a later screen degrades the pair too). Any pair failing a
    condition is omitted — it draws as two date controls, the documented degrade. This is the ONE
    authority on "merged here"; the screen builder, the per-send data, the footers and the inbound
    decode all consult it, so every surface's merge decision agrees and no object value is ever read
    where a scalar is expected.

    ``has_review_page`` disables EVERY merge for the form: a ``review`` page reads each field's scalar
    ``${data.<name>__val}`` carrier back into a text line, and a merged range START's ``__val`` is the
    range OBJECT ``{start-date, end-date}`` — which Meta's text interpolation cannot render (an
    un-publishable / garbled readback). So when the form carries a review page the pair draws as two
    date controls, whose scalar ``__val`` carriers the readback renders correctly (the documented
    degrade).
    """
    if has_review_page:
        return {}
    on_screen = set(this_fields)
    merges: dict[str, str] = {}
    for end in this_fields:
        end_prop = properties.get(end)
        if not isinstance(end_prop, dict):
            continue
        start = end_prop.get("rangeStart")
        if not isinstance(start, str) or start not in on_screen:
            continue
        start_prop = properties.get(start)
        if not isinstance(start_prop, dict) or not _is_date_field(end_prop) or not _is_date_field(start_prop):
            continue
        shared = end_prop.get("visibleWhen")
        if shared != start_prop.get("visibleWhen") or _splits_into_variants(shared):
            # The two ends must share the SAME predicate, and a shared predicate shown for several
            # values (a multi-entry ``in``) draws the pair as two date controls. A pair whose own
            # predicate is single-valued merges even under a controller drawn per case: it then
            # draws one range control per inherited case.
            continue
        if _sibling_depends_on(list(properties), properties, {start, end}):
            continue
        merges[end] = start
    return merges


def _splits_into_variants(predicate: Any) -> bool:
    # Whether a ``visibleWhen`` predicate renders as several per-value cases — a multi-entry ``in``
    # (Meta's ``||`` cannot test membership, so each value becomes its own ``If`` branch). An
    # ``equals``/``notEmpty`` or a single-entry ``in`` is one case.
    return isinstance(predicate, dict) and isinstance(predicate.get("in"), list) and len(predicate["in"]) > 1


def _union_exclusions(start_prop: dict[str, Any], end_prop: dict[str, Any]) -> tuple[list[str], set[str]]:
    # The UNION of both fields' excluded explicit dates (order-preserving, de-duplicated) and excluded
    # weekday names — a merged range control is never looser than either field.
    excluded_dates: list[str] = []
    excluded_days: set[str] = set()
    for prop in (start_prop, end_prop):
        unavailable = prop.get("unavailableDates")
        if not isinstance(unavailable, list):
            continue
        for entry in unavailable:
            if not isinstance(entry, str):
                continue
            if _ISO_DATE_RE.match(entry) and entry not in excluded_dates:
                excluded_dates.append(entry)
            elif entry.lower() in FORM_WEEKDAYS:
                excluded_days.add(entry.lower())
    return excluded_dates, excluded_days


def _merged_date_bounds(start_prop: dict[str, Any], end_prop: dict[str, Any]) -> dict[str, Any]:
    # The bounds a merged range CalendarPicker carries: never looser than either field. ``min-date``
    # is the LATER of the two ``minDate``s, ``max-date`` the EARLIER of the two ``maxDate``s (ISO dates
    # sort lexically), and the excluded dates / weekdays are the UNION of both fields' exclusions.
    out: dict[str, Any] = {}
    min_dates = [p["minDate"] for p in (start_prop, end_prop) if isinstance(p.get("minDate"), str)]
    max_dates = [p["maxDate"] for p in (start_prop, end_prop) if isinstance(p.get("maxDate"), str)]
    if min_dates:
        out["min-date"] = max(min_dates)
    if max_dates:
        out["max-date"] = min(max_dates)
    excluded_dates, excluded_days = _union_exclusions(start_prop, end_prop)
    if excluded_dates:
        out["unavailable-dates"] = excluded_dates
    if excluded_days:
        out["include-days"] = [_WEEKDAY_CODES[day] for day in FORM_WEEKDAYS if day not in excluded_days]
    return out


def _range_span(end_prop: dict[str, Any]) -> dict[str, Any]:
    # ``min-days``/``max-days`` from the END's span. The platform span is the INCLUSIVE day count
    # ``(end - start).days + 1``; Meta's ``min-days``/``max-days`` count the DIFFERENCE, so each is
    # the platform value minus one. A ``minDays`` of 0 or 1 is no real floor and emits no ``min-days``.
    out: dict[str, Any] = {}
    min_days = end_prop.get("minDays")
    max_days = end_prop.get("maxDays")
    if isinstance(min_days, int) and not isinstance(min_days, bool) and min_days > 1:
        out["min-days"] = min_days - 1
    if isinstance(max_days, int) and not isinstance(max_days, bool) and max_days >= 1:
        out["max-days"] = max_days - 1
    return out


def _range_value_type_decl() -> dict[str, Any]:
    """The screen-``data`` type declaration for a merged range control's value — an object of two date strings."""
    return {
        "type": "object",
        "properties": {"start-date": {"type": "string"}, "end-date": {"type": "string"}},
        "__example__": {"start-date": "", "end-date": ""},
    }


def _calendar_range_component(
    start_name: str,
    end_name: str,
    start_prop: dict[str, Any],
    end_prop: dict[str, Any],
    required_start: bool,
    required_end: bool,
    start_cname: str,
) -> dict[str, Any]:
    """A range-mode ``CalendarPicker`` for a ``rangeStart`` pair drawn as ONE control.

    Emitted at the START's position and keyed by the START's component name; the END emits no
    control. ``label`` and ``required`` are each the paired object Meta's range mode takes
    (``{start-date, end-date}``), so the two ends carry their own titles and required flags. The
    ``error-message`` is ONE binding to the START's ``__err`` screen-data key, declared as the object
    ``{start-date, end-date}`` that carries each end's error text (Meta's validator refuses a binding
    per end). The bounds are never looser than either field (:func:`_merged_date_bounds`) and the span rides the
    END's ``minDays``/``maxDays`` (:func:`_range_span`). The value is the object ``{start-date,
    end-date}``; the inbound decode splits it back onto the two schema date keys, so the submitted
    answer stays the two scalar date strings.
    """
    component: dict[str, Any] = {
        "type": "CalendarPicker",
        "name": start_cname,
        "label": {"start-date": _field_label(start_name, start_prop), "end-date": _field_label(end_name, end_prop)},
        "mode": "range",
        "required": {"start-date": required_start, "end-date": required_end},
        "init-value": f"${{data.{start_cname}__init}}",
    }
    component.update(_merged_date_bounds(start_prop, end_prop))
    component.update(_range_span(end_prop))
    component["error-message"] = error_binding(start_cname)
    return component


def split_range_values(properties: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Split a merged range control's object value back onto its two schema date keys.

    A ``rangeStart`` pair merged into one range control returns its value as ``{start-date,
    end-date}`` under the START's schema key; this maps the two members onto the START key and the
    END key (the property whose ``rangeStart`` names the START), leaving the answer the two scalar
    date strings. A pair drawn as two controls carries two scalar values already (no object), so it
    passes through untouched. Unknown/malformed objects are left as-is for the door to refuse.
    """
    out = dict(values)
    for end_name, end_prop in properties.items():
        start = end_prop.get("rangeStart") if isinstance(end_prop, dict) else None
        if not isinstance(start, str):
            continue
        obj = out.get(start)
        if isinstance(obj, dict) and ("start-date" in obj or "end-date" in obj):
            out[start] = obj.get("start-date", "")
            out[end_name] = obj.get("end-date", "")
    return out


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


def _text_input_component(
    name: str, cname: str, label: str, required: bool, prop: dict[str, Any], init: str
) -> dict[str, Any]:
    """A ``TextInput`` for a ``string`` field, drawing the keyboard its ``inputMode`` names.

    The platform ``inputMode`` hint (the open HTML ``inputmode`` vocabulary) selects Meta's
    ``input-type``: ``numeric`` and ``decimal`` draw the numeric keypad (``number`` — its keypad
    admits a decimal separator), ``tel`` the phone keypad (``phone``), ``email`` the email keyboard
    (``email``). ``url`` has no Meta ``input-type`` (its set is text|number|email|password|passcode|
    phone), so it DEGRADES to the plain text field here — named explicitly below, never a silent
    fallthrough; the kind is still honoured on surfaces that draw it. ``text`` and an absent
    ``inputMode`` are the plain text field, byte-identical to a string field carrying no hint.

    ``inputMode`` only chooses the keyboard: the value always arrives as the typed string and is
    never coerced. The map is exhaustive over :class:`FormInputMode` (``assert_never``), so a value
    added to the platform later is a type error here until this channel decides its rendering. A
    value the enum cannot construct raises :class:`ChannelInputError` naming the property.

    A declared ``maxLength``/``minLength`` is drawn as Meta's ``max-chars``/``min-chars`` on the
    single-line field (Meta otherwise caps an unbounded ``TextInput`` at 80 characters), so an
    author's length bound holds on this surface instead of being silently cut to the vendor default.
    """
    plain: dict[str, Any] = {"type": "TextInput", "name": cname, "label": label, "required": required}
    length = _length_attrs(prop)
    if prop.get("format") == "email":
        # ``format: email`` draws the email keyboard (the subset refuses an ``inputMode`` beside it);
        # the address shape itself is checked at the data endpoint.
        return {**plain, "input-type": "email", **length, "init-value": init}
    raw = prop.get("inputMode")
    if raw is None:
        return {**plain, **length, "init-value": init}
    try:
        mode = FormInputMode(raw)
    except ValueError as exc:
        raise ChannelInputError(
            f"form property {name!r}: unsupported inputMode {raw!r} — a text field allows only "
            "text, numeric, decimal, tel, email, url"
        ) from exc
    match mode:
        case FormInputMode.TEXT:
            return {**plain, **length, "init-value": init}
        case FormInputMode.URL:
            # Meta's TextInput has no ``url`` input-type, so ``url`` degrades to the plain text field.
            return {**plain, **length, "init-value": init}
        case FormInputMode.NUMERIC | FormInputMode.DECIMAL:
            # Meta's numeric keypad (``input-type: number``) admits a decimal separator, so both
            # the whole-number and decimal keyboards map to it.
            input_type = "number"
        case FormInputMode.TEL:
            input_type = "phone"
        case FormInputMode.EMAIL:
            input_type = "email"
        case _:
            assert_never(mode)
    return {**plain, "input-type": input_type, **length, "init-value": init}


def _length_attrs(prop: dict[str, Any]) -> dict[str, Any]:
    """Meta's ``max-chars``/``min-chars`` from a string property's ``maxLength``/``minLength``, when declared."""
    attrs: dict[str, Any] = {}
    max_length = prop.get("maxLength")
    min_length = prop.get("minLength")
    if isinstance(max_length, int) and not isinstance(max_length, bool):
        attrs["max-chars"] = max_length
    if isinstance(min_length, int) and not isinstance(min_length, bool):
        attrs["min-chars"] = min_length
    return attrs


def _textarea_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A multi-line ``TextArea`` for a ``control: textarea`` string field.

    Meta's ``TextArea`` carries ``max-length`` (drawn from a declared ``maxLength``, else Meta
    defaults it to 600 characters) but has no ``input-type`` and no ``min-length`` — so a
    ``textarea`` never carries an ``inputMode`` (the schema refuses the combination) and its only
    drawn bound is the maximum.
    """
    component: dict[str, Any] = {"type": "TextArea", "name": cname, "label": label, "required": required}
    max_length = prop.get("maxLength")
    if isinstance(max_length, int) and not isinstance(max_length, bool):
        component["max-length"] = max_length
    component["init-value"] = init
    return component


def _choice_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """The Flow control a dynamic (``data-source``) choice property renders as.

    A ``control: chips`` choice renders as a ``ChipsSelector`` for Meta's 2..20 choices, else
    DEGRADES to the shape's default control (named, documented); a property with no ``chips`` draws
    the default. The default is a ``CheckboxGroup`` for an array of strings, and for a string a
    ``RadioButtonsGroup`` (short enum) / ``Dropdown``.
    """
    if _draws_chips(prop):
        return _chips_component(cname, label, required, prop, init)
    if _is_array_of_strings(prop):
        return _checkbox_component(cname, label, required, prop, init)
    return {
        "type": _choice_component_type(prop),
        "name": cname,
        "label": label,
        "required": required,
        "data-source": f"${{data.{cname}__ds}}",
        "init-value": init,
    }


def _string_component(
    name: str, cname: str, label: str, required: bool, prop: dict[str, Any], init: str
) -> dict[str, Any]:
    """The control a plain (non-choice, non-date) ``string`` field renders as.

    A ``control: textarea`` draws a multi-line ``TextArea``; no control draws the single-line
    ``TextInput`` (whose keyboard its ``inputMode`` selects). A ``chips`` control never reaches
    here — a chips string carries an ``enum`` and so is a choice field drawn above — so it is a
    schema inconsistency and refused loudly rather than silently drawn as plain text. The match is
    exhaustive over :class:`FormControl` (``assert_never``), so a control added to the platform later
    is a type error here until this channel decides how a plain string draws it.
    """
    raw = prop.get("control")
    if raw is None:
        return _text_input_component(name, cname, label, required, prop, init)
    control = FormControl(raw)
    match control:
        case FormControl.TEXTAREA:
            return _textarea_component(cname, label, required, prop, init)
        case FormControl.CHIPS:
            raise ChannelInputError(
                f"form property {name!r}: control 'chips' needs a declared choice list (an enum); a "
                "plain string carries none"
            )
        case _:
            assert_never(control)


def _dynamic_component(
    name: str,
    prop: dict[str, Any],
    required: bool,
    option_fields: set[str],
    names: dict[str, str],
    invalid: str | None = None,
) -> dict[str, Any]:
    """One Flow field component for the dynamic (per-send) form.

    The component ``name`` and its data references are the identifier-safe name ``names``
    maps the schema key to; the label keeps the property ``title`` (else the raw property
    name). Every control reads its ``init-value`` from the screen ``data`` model and a
    choice field reads a dynamic ``data-source``, so the send injects the values/options. An
    array of strings renders a CheckboxGroup, a short string enum a RadioButtonsGroup (else a
    Dropdown), and a date field with platform constraints a bounded CalendarPicker. Raises
    ``ChannelInputError`` naming a property outside the supported subset. A field's second line
    (its schema ``description``) rides the control as ``helper-text`` where Meta allows it
    (:func:`_with_helper_text`); elsewhere :func:`field_caption` draws it beside the control.

    A shape Meta checks with a ``pattern`` (:func:`~tai42_channel_whatsapp.flows_shapes.native_pattern`)
    is drawn on the control with the ``helper-text`` Meta then requires — the field's second line,
    else the caller's ``invalid`` sentence, else this channel's helper word for the shape. A control that
    can show an error binds ``error-message`` to its ``__err`` screen-data key.
    """
    component = _with_helper_text(name, _field_control(name, prop, required, option_fields, names), prop)
    drawn = native_pattern(prop, component)
    if drawn is not None:
        component["pattern"] = drawn.pattern
        if "helper-text" not in component:
            helper = invalid or helper_word(drawn.kind, drawn.params)
            if len(helper) > _HELPER_TEXT_MAX_CHARS:
                raise ChannelInputError(
                    f"form property {name!r} invalid sentence is {len(helper)} characters; WhatsApp caps the "
                    f"helper text a pattern field requires at {_HELPER_TEXT_MAX_CHARS}"
                )
            component["helper-text"] = helper
    if component["type"] in ERROR_MESSAGE_TYPES:
        component["error-message"] = error_binding(names[name])
    return component


def _field_control(
    name: str, prop: dict[str, Any], required: bool, option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """The bare Flow control for one field (see :func:`_dynamic_component`), before its second line."""
    label = _field_label(name, prop)
    cname = names[name]
    prop_type = prop.get("type")
    init = f"${{data.{cname}__init}}"
    if _is_array_of_strings(prop):
        if _choice_options_enum(prop) is None and name not in option_fields:
            raise ChannelInputError(
                f"form property {name!r}: a multiple-choice (array of strings) field needs an 'items' enum "
                "or per-send options to render as a CheckboxGroup"
            )
        return _choice_component(cname, label, required, prop, init)
    if _is_choice_field(name, prop, option_fields):
        return _choice_component(cname, label, required, prop, init)
    if _is_date_field(prop):
        return _calendar_component(cname, label, required, prop, init)
    if prop_type == "string":
        return _string_component(name, cname, label, required, prop, init)
    if prop_type == "boolean":
        return {"type": "OptIn", "name": cname, "label": label, "required": required, "init-value": init}
    if prop_type in ("integer", "number"):
        return {
            "type": "TextInput",
            "name": cname,
            "label": label,
            "required": required,
            "input-type": "number",
            "init-value": init,
        }
    raise ChannelInputError(
        f"form property {name!r}: unsupported schema type {prop_type!r} — a form field must be "
        "string, string+enum, an array of strings, boolean, integer, or number (no nested objects, "
        "arrays of non-strings, or unions)"
    )


def _value_type_decl(prop: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` type declaration for a field's value.

    Boolean for an OptIn, array for a CheckboxGroup, else a string.
    Used both for a field's ``init-value`` source and for a value collected on an earlier
    screen and forwarded to completion, so the declared type always matches the control.
    """
    prop_type = prop.get("type")
    if prop_type == "boolean":
        return {"type": "boolean", "__example__": False}
    if prop_type == "array" or _chips_single_pick(prop):
        # A single-pick ChipsSelector carries a one-element LIST, so a ``string`` chips field's
        # value is array-typed exactly like a multiple-choice field (decoded back to the string).
        return {"type": "array", "items": {"type": "string"}, "__example__": []}
    return {"type": "string", "__example__": ""}


def _field_data_decls(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str], merged_start: bool = False
) -> dict[str, Any]:
    """The screen-``data`` declarations a field needs where it is RENDERED.

    Its ``init-value`` source and, for a choice field, its dynamic ``data-source`` —
    keyed by the field's identifier-safe component name. A merged range START declares its
    ``init`` as the range object (two date strings) and carries no ``data-source``.
    """
    cname = names[name]
    init_decl = _range_value_type_decl() if merged_start else _value_type_decl(prop)
    decls: dict[str, Any] = {f"{cname}__init": init_decl}
    if not merged_start and _is_choice_field(name, prop, option_fields):
        chips = _draws_chips(prop)
        decls[f"{cname}__ds"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": copy.deepcopy(_CHIPS_DS_ITEM_PROPERTIES if chips else _DS_ITEM_PROPERTIES),
            },
            "__example__": copy.deepcopy(_CHIPS_DS_ITEM_EXAMPLE if chips else _DS_ITEM_EXAMPLE),
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
