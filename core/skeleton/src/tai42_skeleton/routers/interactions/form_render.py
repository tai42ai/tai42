"""Server-side rendering of a channel-delivered form question's schema into an escaped HTML page.

The one HTML fallback surface a channel-delivered form is answered on. It renders every field
kind the channel subset admits — scalars, a choice as a radio group (short lists) or a select
(long lists), a multiple-choice array as a checkbox group, a choice drawn as a chip grid
(``control: chips``), a multi-line text control (``control: textarea``) with its length bound
shown and enforced, a date with its native min/max bounds — plus ordered display blocks
(heading/body/image), a review/summary step, and conditional show/hide driven by each field's
``visibleWhen`` predicate. Where a surface cannot draw a thing it DEGRADES by a documented rule,
never silently: an image with no static source shows its alt text; arbitrary unavailable days
cannot be disabled on a native date input, so the client best-effort-validates them at submit
and the server enforces them regardless. A ``rangeStart`` pair IS the platform's date range:
where it is drawable as one (both ends on the page, both ``format: date``, the same
``visibleWhen``, and no other field's ``visibleWhen`` names either end) it draws as ONE
``<fieldset>`` grouping the two date inputs, labelled by the START's and END's titles; otherwise
it draws as two ordinary date fields (a documented degrade). A ``timeOf`` time draws right after
its date (the shared drawing order): inside the group, under its date, when the pair is drawn as
one. The answer is always the plain date and time strings — nothing is recombined.

The page's own chrome carries the caller's words where set, off the per-send ``wording`` catalogue:
``wording.form.title`` is the page title and heading (else "Respond"), each page's primary button
reads its ``button_label`` (a page-less form's submit button reads ``wording.form.submit``, else
"Next"/"Submit"), and ``wording.form.back`` labels the Back button (else "Back"). The form page's
own validation/error sentences (``wording.answer_page.form.*``), the could-not-update sentence
(``wording.form.update_failed``), the refusal sentence (``wording.answer.rejected``) and the
submitted-state copy (``wording.answer_page.submitted``) ride escaped ``data-*`` attributes the
constant submit script reads; absent, each shows its own word. Each field's words
(``wording.form.fields[<name>]``) ride its control: the ``required`` sentence the script shows for a
missing required value (else the browser's own prompt), the ``length_hint`` ceiling line of a text
control (else "up to {max} characters") and the ``true_label`` / ``false_label`` the review readback
draws for a boolean (else ``true`` / ``false``).
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any, assert_never

from tai42_contract.interactions.models import FormControl, FormInputMode
from tai42_kit.interactions.form_vocabulary import RADIO_OPTION_THRESHOLD, drawing_order

from tai42_skeleton.interactions.form_schema import channel_form_fields, paired_time

from .form_shape_attrs import _attr, _field_words, _number_bounds, _required_attr, _shape_attrs, _word
from .pages import _FORM_SUBMIT_SCRIPT


def _text_input_attrs(prop: dict[str, Any]) -> str:
    """The HTML ``type``/``inputmode`` attributes for a plain string field, from its ``inputMode``.

    ``numeric``/``decimal`` keep ``type="text"`` with an ``inputmode`` hint so the value stays the
    typed string (a native ``type="number"`` would strip a leading zero); the rest pick the native
    input type. ``data-kind="string"`` is kept by the caller for every kind, so the submit script
    posts the typed string whatever the keyboard. Absent means a plain ``type="text"``. The schema
    is pre-validated by ``channel_form_fields``, so the value is one of the known kinds; the match
    is exhaustive, so a kind added later is a type error here until it is drawn.
    """
    if prop.get("format") == "email":
        return 'type="email"'
    mode = prop.get("inputMode")
    if mode is None:
        return 'type="text"'
    match FormInputMode(mode):
        case FormInputMode.TEXT:
            return 'type="text"'
        case FormInputMode.NUMERIC:
            return 'type="text" inputmode="numeric"'
        case FormInputMode.DECIMAL:
            return 'type="text" inputmode="decimal"'
        case FormInputMode.TEL:
            return 'type="tel"'
        case FormInputMode.EMAIL:
            return 'type="email"'
        case FormInputMode.URL:
            return 'type="url"'
        case _ as unreachable:
            assert_never(unreachable)


class _FormRenderError(Exception):
    """Raised when a stored form question's schema cannot be rendered into a page.

    The schema is outside the channel-deliverable subset (``form_schema``). ``ask``
    refuses such a schema before persisting, so a form record that reaches the GET
    door MUST render; failing to is a server bug (a record that bypassed
    ``ask``), so it surfaces as a loud 500 with a logged reason, never a blank
    or half-rendered page silently dropping fields.
    """


def _second_line(text: Any) -> str | None:
    # A non-blank secondary line (a field's or an option's ``description``), else None. A
    # blank/whitespace-only value counts as absent — the same rule a blank ``title`` gets.
    return text if isinstance(text, str) and text.strip() else None


def _string_choices(
    prop: dict[str, Any], options: list[dict[str, Any]] | None
) -> list[tuple[str, str, str | None]] | None:
    # The ``(value, label, description)`` choice triples for a string property: the per-send option
    # list when given (it replaces the enum for this send), else the property's own ``enum``, else
    # None (a free-text string). A schema ``enum`` carries no labels or second lines.
    if options is not None:
        return [
            (str(option["value"]), str(option.get("label") or option["value"]), _second_line(option.get("description")))
            for option in options
        ]
    enum = prop.get("enum")
    if enum is not None:
        return [(str(choice), str(choice), None) for choice in enum]
    return None


def _array_choices(
    prop: dict[str, Any], options: list[dict[str, Any]] | None
) -> list[tuple[str, str, str | None]] | None:
    # The choice triples for an array-of-strings property: the per-send option list, else the
    # string ``items`` enum, else None (a free list the human types one entry per line).
    if options is not None:
        return [
            (str(option["value"]), str(option.get("label") or option["value"]), _second_line(option.get("description")))
            for option in options
        ]
    items = prop.get("items")
    enum = items.get("enum") if isinstance(items, dict) else None
    if isinstance(enum, list):
        return [(str(choice), str(choice), None) for choice in enum]
    return None


def _option_text(label: str, description: str | None) -> str:
    # A single-line option caption for a control that has no second-line slot (a ``<select>``
    # row): the second line is folded onto the label after an em dash so its content is never
    # dropped. A control that CAN draw a second line (radio/checkbox/chip) uses ``description`` directly.
    if description is None:
        return html.escape(label)
    return f"{html.escape(label)} — {html.escape(description)}"


def _string_select(
    esc_name: str,
    req_attr: str,
    choices: list[tuple[str, str, str | None]],
    value: Any,
    placeholder: str | None = None,
) -> str:
    # A ``<select>`` over ``(value, label, description)`` triples: a leading blank option lets an
    # optional select stay empty and forces a required one to a real choice on submit. Its label
    # is the caller's ``wording.form.fields[name].choice_label`` when set, else a dash. A native
    # ``<option>`` has no second-line slot, so a description folds onto the option text.
    blank_label = html.escape(placeholder) if placeholder else "—"
    parts = [f'<option value="">{blank_label}</option>']
    for opt_value, opt_label, opt_desc in choices:
        selected = " selected" if value is not None and str(value) == opt_value else ""
        parts.append(f'<option value="{_attr(opt_value)}"{selected}>{_option_text(opt_label, opt_desc)}</option>')
    return f'<select data-field="{esc_name}" data-kind="string"{req_attr}>' + "".join(parts) + "</select>"


def _choice_second_line(description: str | None) -> str:
    # The ``<small>`` second line under a radio/checkbox/chip option label, or empty when absent.
    return f'<small class="choice-desc">{html.escape(description)}</small>' if description is not None else ""


def _string_radio(esc_name: str, req_attr: str, choices: list[tuple[str, str, str | None]], value: Any) -> str:
    # A radio group over ``(value, label, description)`` triples — the short-list control.
    # ``required`` on every radio makes the browser demand one selection in the group; a
    # description renders as a ``<small>`` second line under the option label.
    parts = []
    for opt_value, opt_label, opt_desc in choices:
        checked = " checked" if value is not None and str(value) == opt_value else ""
        parts.append(
            f'<label class="choice"><input type="radio" name="{esc_name}" data-field="{esc_name}" '
            f'data-kind="string" value="{_attr(opt_value)}"{checked}{req_attr}> {html.escape(opt_label)}'
            f"{_choice_second_line(opt_desc)}</label>"
        )
    return "".join(parts)


def _checkbox_group(esc_name: str, choices: list[tuple[str, str, str | None]], value: Any) -> str:
    # A checkbox group for an array-of-strings (multiple-choice) field: each checked box
    # contributes its value to the submitted list; a description renders as a ``<small>`` second
    # line under the option label.
    selected = {str(item) for item in value} if isinstance(value, list) else set()
    parts = []
    for opt_value, opt_label, opt_desc in choices:
        checked = " checked" if opt_value in selected else ""
        parts.append(
            f'<label class="choice"><input type="checkbox" data-field="{esc_name}" data-kind="array" '
            f'value="{_attr(opt_value)}"{checked}> {html.escape(opt_label)}{_choice_second_line(opt_desc)}</label>'
        )
    return "".join(parts)


def _chip_group(
    esc_name: str, req_attr: str, choices: list[tuple[str, str, str | None]], value: Any, *, multi: bool
) -> str:
    # A choice drawn as a chip grid: tap-to-select pills over ``(value, label, description)``
    # triples. Each pill is a ``label.choice.chip`` whose input is hidden by the page CSS and tinted
    # when checked; a description renders as a ``<small>`` second line inside the pill. A
    # string chooses ONE (radios, ``data-kind="string"``), an array of strings several (checkboxes,
    # ``data-kind="array"``). The ``choice`` class and the ``data-field``/``data-kind`` markup match
    # the radio/checkbox group, so the submit script and its ``rebuildChoices`` read a chip field
    # exactly as they read the default control.
    input_type = "checkbox" if multi else "radio"
    kind = "array" if multi else "string"
    name_attr = "" if multi else f' name="{esc_name}"'
    selected = {str(item) for item in value} if multi and isinstance(value, list) else set()
    parts = []
    for opt_value, opt_label, opt_desc in choices:
        if multi:
            checked = " checked" if opt_value in selected else ""
            req = ""
        else:
            checked = " checked" if value is not None and str(value) == opt_value else ""
            req = req_attr
        parts.append(
            f'<label class="choice chip"><input type="{input_type}"{name_attr} data-field="{esc_name}" '
            f'data-kind="{kind}" value="{_attr(opt_value)}"{checked}{req}> {html.escape(opt_label)}'
            f"{_choice_second_line(opt_desc)}</label>"
        )
    return "".join(parts)


def _length_bounds(prop: dict[str, Any], length_hint: str | None) -> tuple[str, str]:
    # The native ``maxlength``/``minlength`` attributes a text control draws for a property's
    # ``maxLength``/``minLength`` (the browser's ``checkValidity`` enforces them, the answer door
    # enforces them again), and a small hint that SHOWS the maximum so the human sees the ceiling:
    # the caller's ``length_hint`` with ``{max}`` replaced by the maximum when set, else "up to
    # {max} characters". Drawn on every text control — the single-line input and the multi-line
    # text area alike.
    attrs = ""
    max_length = prop.get("maxLength")
    min_length = prop.get("minLength")
    if isinstance(max_length, int):
        attrs += f' maxlength="{max_length}"'
    if isinstance(min_length, int):
        attrs += f' minlength="{min_length}"'
    if not isinstance(max_length, int):
        return attrs, ""
    line = (
        length_hint.replace("{max}", str(max_length)) if length_hint is not None else f"up to {max_length} characters"
    )
    return attrs, f'<small class="hint">{html.escape(line)}</small>'


def _textarea_control(
    esc_name: str, req_attr: str, prop: dict[str, Any], value: Any, shape: str, length_hint: str | None
) -> str:
    # A multi-line text control. ``data-kind="string"`` is kept so the submit script posts the
    # typed string; the length bounds and the ceiling hint are drawn by :func:`_length_bounds`, the
    # shape attributes by :func:`_shape_attrs`.
    bounds, hint = _length_bounds(prop, length_hint)
    content = html.escape(str(value)) if value is not None else ""
    control = (
        f'<textarea data-field="{esc_name}" data-kind="string" rows="4"{bounds}{req_attr}{shape}>{content}</textarea>'
    )
    return control + hint


def _date_range_group(
    start: str,
    end: str,
    field_by_name: dict[str, tuple[dict[str, Any], bool]],
    values: Any,
    field_wording: dict[str, Any],
    times: dict[str, str],
) -> str:
    # A drawable ``rangeStart`` pair as ONE ``<fieldset>`` under a legend naming both ends: one
    # column per end, side by side, each holding its native date input (its own ``data-field``,
    # bounds, unavailable days, ``required``, prefilled value and second line under its label) and,
    # under it, the drawn control of that end's ``timeOf`` time when ``times`` holds one (date name
    # -> the time's labelled control). The submit script collects each ``data-field`` exactly as it
    # does separate inputs — the answer stays the plain date and time strings.
    properties = {name: prop for name, (prop, _required) in field_by_name.items()}

    def column(name: str) -> str:
        prop, required = field_by_name[name]
        words = _field_words(field_wording, name)
        label = html.escape(str(prop.get("title") or name))
        date_input = _date_input(
            _attr(name),
            _required_attr(required, words),
            prop,
            values.get(name),
            _shape_attrs(prop, _word(words, "invalid"), properties),
        )
        hint = _field_hint(_second_line(prop.get("description")))
        return f'<div class="range-end"><label>{label}<br>{hint}{date_input}</label>{times.get(name, "")}</div>'

    start_label = html.escape(str(field_by_name[start][0].get("title") or start))
    end_label = html.escape(str(field_by_name[end][0].get("title") or end))
    legend = f"{start_label} {chr(0x2013)} {end_label}"  # an en dash between the two end labels
    return f'<fieldset class="daterange"><legend>{legend}</legend>{column(start)}{column(end)}</fieldset>'


def _page_times(start: str, end: str, names: list[str], properties: dict[str, Any]) -> dict[str, str]:
    # Each end of a pair mapped to its ``timeOf`` time, for the ends whose time is on this page.
    times: dict[str, str] = {}
    for date_name in (start, end):
        time_name = paired_time(date_name, properties)
        if time_name is not None and time_name in names:
            times[date_name] = time_name
    return times


def _drawable_date_ranges(names: list[str], field_by_name: dict[str, tuple[dict[str, Any], bool]]) -> dict[str, str]:
    # The ``rangeStart`` pairs drawable as ONE control among ``names`` (the fields on one page),
    # mapped START name -> END name. A pair draws as one only when: both ends are on this page;
    # both are ``format: date``; they carry the SAME ``visibleWhen`` (both absent or deep-equal);
    # and no OTHER field on the page has a ``visibleWhen`` naming either end (else a sibling's
    # conditional would reference a control the merge dissolves, so the pair stays two fields). Any
    # pair failing a condition is omitted here and drawn as two ordinary date fields.
    name_set = set(names)
    merged: dict[str, str] = {}
    for end in names:
        end_prop = field_by_name[end][0]
        start = end_prop.get("rangeStart")
        if not isinstance(start, str) or start not in name_set:
            continue
        start_prop = field_by_name[start][0]
        if end_prop.get("format") != "date" or start_prop.get("format") != "date":
            continue
        if end_prop.get("visibleWhen") != start_prop.get("visibleWhen"):
            continue
        blocked = False
        for other in names:
            if other in (start, end):
                continue
            other_vw = field_by_name[other][0].get("visibleWhen")
            if isinstance(other_vw, dict) and other_vw.get("field") in (start, end):
                blocked = True
                break
        if not blocked:
            merged[start] = end
    return merged


def _date_input(esc_name: str, req_attr: str, prop: dict[str, Any], value: Any, shape: str) -> str:
    # A native date input carrying its min/max bounds (the picker restricts the range), its shape
    # attributes (:func:`_shape_attrs` — a range END's start and span among them) and, as a
    # best-effort client hint, the unavailable dates/weekdays the submit script checks — the
    # server enforces them regardless.
    bounds = ""
    if isinstance(prop.get("minDate"), str):
        bounds += f' min="{_attr(prop["minDate"])}"'
    if isinstance(prop.get("maxDate"), str):
        bounds += f' max="{_attr(prop["maxDate"])}"'
    unavailable = prop.get("unavailableDates")
    if isinstance(unavailable, list):
        bounds += f' data-unavailable="{_attr(json.dumps(unavailable))}"'
    val_attr = f' value="{_attr(value)}"' if value is not None else ""
    return f'<input data-field="{esc_name}" data-kind="string" type="date"{bounds}{val_attr}{req_attr}{shape}>'


def _field_hint(hint: str | None) -> str:
    # A field's second line (its schema ``description``) as a ``<small class="hint">``, or empty.
    return f'<small class="hint">{html.escape(hint)}</small>' if hint is not None else ""


def _labelled(
    esc_label: str,
    control: str,
    *,
    grouped: bool,
    group_attr: str = "",
    fieldset_class: str = "",
    hint: str | None = None,
) -> str:
    # Wrap a control in its label — a ``<fieldset>``/``<legend>`` for a radio/checkbox/chip group
    # (one label cannot own several inputs), a ``<label>`` for a single control. ``fieldset_class``
    # names the group's style (``chips`` for a chip grid) so the page CSS can draw it as pills. A
    # field's second line (its schema ``description``) renders as a ``<small class="hint">``
    # between the label text and the control.
    hint_html = _field_hint(hint)
    if grouped:
        cls = f' class="{fieldset_class}"' if fieldset_class else ""
        return f"<fieldset{cls}{group_attr}><legend>{esc_label}</legend>{hint_html}{control}</fieldset>"
    return f"<label>{esc_label}<br>{hint_html}{control}</label>"


def _render_array_field(
    esc_name: str,
    esc_label: str,
    req_attr: str,
    prop: dict[str, Any],
    is_required: bool,
    value: Any,
    options: list[dict[str, Any]] | None,
    hint: str | None,
    required_sentence: str | None,
) -> str:
    # An array-of-strings property: a chip grid (``control: chips``) or a checkbox group over its
    # choices, else a one-entry-per-line free text area when it declares none. ``hint`` is the
    # field's second line, drawn under its label. A required group carries the caller's
    # ``required_sentence`` as ``data-required`` when set.
    choices = _array_choices(prop, options)
    if choices is not None:
        group_attr = ""
        if is_required:
            group_attr = ' data-array-required="1"'
            if required_sentence is not None:
                group_attr += f' data-required="{_attr(required_sentence)}"'

        if prop.get("control") == FormControl.CHIPS:
            return _labelled(
                esc_label,
                _chip_group(esc_name, req_attr, choices, value, multi=True),
                grouped=True,
                group_attr=group_attr,
                fieldset_class="chips",
                hint=hint,
            )
        return _labelled(
            esc_label, _checkbox_group(esc_name, choices, value), grouped=True, group_attr=group_attr, hint=hint
        )
    text = "\n".join(str(item) for item in value) if isinstance(value, list) else ""
    control = f'<textarea data-field="{esc_name}" data-kind="arraytext"{req_attr}>{html.escape(text)}</textarea>'
    return _labelled(esc_label, control, grouped=False, hint=hint)


def _render_string_field(
    esc_name: str,
    esc_label: str,
    req_attr: str,
    prop: dict[str, Any],
    value: Any,
    options: list[dict[str, Any]] | None,
    hint: str | None,
    choice_label: str | None,
    shape: str,
    length_hint: str | None,
) -> str:
    # A string property: a choice (chips / radio group / select), a multi-line text area
    # (``control: textarea``), a native date input, or a plain text input keyed by its format or
    # inputMode. ``hint`` is the field's second line, drawn under its label; ``shape`` the
    # control's shape attributes (:func:`_shape_attrs`); ``length_hint`` the caller's ceiling line.
    choices = _string_choices(prop, options)
    if choices is not None:
        if prop.get("control") == FormControl.CHIPS:
            return _labelled(
                esc_label,
                _chip_group(esc_name, req_attr, choices, value, multi=False),
                grouped=True,
                fieldset_class="chips",
                hint=hint,
            )
        if len(choices) <= RADIO_OPTION_THRESHOLD:
            return _labelled(esc_label, _string_radio(esc_name, req_attr, choices, value), grouped=True, hint=hint)
        return _labelled(
            esc_label, _string_select(esc_name, req_attr, choices, value, choice_label), grouped=False, hint=hint
        )
    if prop.get("control") == FormControl.TEXTAREA:
        return _labelled(
            esc_label, _textarea_control(esc_name, req_attr, prop, value, shape, length_hint), grouped=False, hint=hint
        )
    if prop.get("format") == "date":
        return _labelled(esc_label, _date_input(esc_name, req_attr, prop, value, shape), grouped=False, hint=hint)
    val_attr = f' value="{_attr(value)}"' if value is not None else ""
    bounds, ceiling = _length_bounds(prop, length_hint)
    attrs = f"{_text_input_attrs(prop)}{bounds}{val_attr}{req_attr}{shape}"
    control = f'<input data-field="{esc_name}" data-kind="string" {attrs}>{ceiling}'
    return _labelled(esc_label, control, grouped=False, hint=hint)


def _render_field(
    name: str,
    prop: dict[str, Any],
    is_required: bool,
    *,
    value: Any = None,
    options: list[dict[str, Any]] | None = None,
    words: Mapping[str, Any],
    properties: dict[str, Any],
) -> str:
    """Render one subset-validated schema property into an escaped form control.

    A choice (``enum`` or per-send ``options``) renders as a radio group at or below
    :data:`RADIO_OPTION_THRESHOLD` options, a ``<select>`` above it. An array of strings renders
    a checkbox group over its choices (or a one-entry-per-line text area when it declares none).
    A ``format: date`` string renders a native date input with its min/max bounds; ``format:
    email`` the native email input; ``time`` / ``date-time`` render as text (no native control
    emits their value shape). A plain string's ``inputMode`` picks the keyboard (``email``/``url``/
    ``tel`` -> that input type; ``numeric`` / ``decimal`` -> ``type="text"`` with an ``inputmode``
    hint so the value stays the typed string), ``data-kind="string"`` kept for every kind.
    ``boolean`` -> checkbox, ``integer``/``number`` -> number input with its native ``min``/``max``.
    Every text, date and number control carries its shape attributes (:func:`_shape_attrs`) — the
    caller's ``invalid`` sentence among them; ``properties`` is the form's properties by name, read
    for the attributes that name another field. ``words`` is the field's
    ``wording.form.fields[name]`` entry: its ``choice_label`` heads a select, its ``required``
    rides every required control, its ``length_hint`` is the ceiling line of a text control, and
    its ``true_label`` / ``false_label`` ride the checkbox for the review readback. The property is pre-validated by
    ``channel_form_fields``; an unexpected type is a server bug and raises ``_FormRenderError``.
    """
    esc_name = _attr(name)
    esc_label = html.escape(str(prop.get("title") or name))
    hint = _second_line(prop.get("description"))
    req_attr = _required_attr(is_required, words)
    shape = _shape_attrs(prop, _word(words, "invalid"), properties)
    ptype = prop.get("type")
    if ptype == "array":
        return _render_array_field(
            esc_name, esc_label, req_attr, prop, is_required, value, options, hint, _word(words, "required")
        )
    if ptype == "string":
        return _render_string_field(
            esc_name,
            esc_label,
            req_attr,
            prop,
            value,
            options,
            hint,
            _word(words, "choice_label"),
            shape,
            _word(words, "length_hint"),
        )
    if ptype == "boolean":
        # A checkbox always submits a boolean (checked/unchecked), so the field is always
        # present — no ``required`` attribute, which would force it checked. The caller's yes/no
        # words ride ``data-true-label`` / ``data-false-label`` for the review readback.
        checked = " checked" if value is True else ""
        labels = "".join(
            f' data-{attr}="{_attr(word)}"'
            for attr, word in (("true-label", _word(words, "true_label")), ("false-label", _word(words, "false_label")))
            if word is not None
        )
        control = f'<input data-field="{esc_name}" data-kind="boolean" type="checkbox"{labels}{checked}>'
        return _labelled(esc_label, control, grouped=False, hint=hint)
    if ptype in ("integer", "number"):
        step = ' step="1"' if ptype == "integer" else ' step="any"'
        val_attr = f' value="{_attr(value)}"' if value is not None else ""
        control = (
            f'<input data-field="{esc_name}" data-kind="number" type="number"{step}{_number_bounds(prop)}'
            f"{val_attr}{req_attr}{shape}>"
        )
        return _labelled(esc_label, control, grouped=False, hint=hint)
    raise _FormRenderError(f"form schema property {name!r} has unsupported type {ptype!r}")


def _render_display(block: dict[str, Any]) -> str:
    """Render one ordered display block (heading/body/image) into escaped HTML.

    An image with a static ``src`` renders an ``<img>`` (its ``alt`` the text a surface that
    cannot draw it shows); a slotted image with no static source DEGRADES to its alt text, never
    a broken image. A ``slot`` rides a ``data-slot`` attribute (the live channel fills it; the
    static page shows the given/empty content).
    """
    kind = block.get("kind")
    slot = block.get("slot")
    slot_attr = f' data-slot="{_attr(slot)}"' if isinstance(slot, str) else ""
    if kind == "image":
        src = block.get("src")
        alt = str(block.get("alt") or "")
        if src:
            return f'<img{slot_attr} src="{_attr(src)}" alt="{_attr(alt)}">'
        return f'<p{slot_attr} class="display-alt">{html.escape(alt)}</p>'
    text = str(block.get("text") or "")
    if kind == "heading":
        return f"<h3{slot_attr}>{html.escape(text)}</h3>"
    return f'<p{slot_attr} class="display-body">{html.escape(text)}</p>'


def _field_row(name: str, prop: dict[str, Any], inner: str) -> str:
    # Wrap one field in a row carrying its ``visibleWhen`` predicate (when any), so the submit
    # script shows/hides it on the submitted values and drops a hidden field from the answer.
    predicate = prop.get("visibleWhen")
    vw_attr = f' data-visible-when="{_attr(json.dumps(predicate))}"' if isinstance(predicate, dict) else ""
    return f'<div class="field" data-field-row="{_attr(name)}"{vw_attr}>{inner}</div>'


def _reactions_attr(reactions: Any) -> str:
    """The ``data-reactions`` attribute for the form when it declares at least one trigger, else ``""``.

    Carries the three event triggers the submit script reads — the fields whose change fires
    a reaction, the pages (by title) whose advance fires one, and whether submission is
    checked by one — so the script posts an on-change / page-advance / submit round-trip to
    the ticket react door and applies the returned update. A static form (no reactions, or a
    reactions block with no trigger) carries no attribute and posts nothing.
    """
    if not isinstance(reactions, dict):
        return ""
    trig = {
        "field_changed": [str(f) for f in reactions.get("field_changed") or []],
        "page_advanced": [str(p) for p in reactions.get("page_advanced") or []],
        "submitted": bool(reactions.get("submitted")),
    }
    if not (trig["field_changed"] or trig["page_advanced"] or trig["submitted"]):
        return ""
    return f' data-reactions="{_attr(json.dumps(trig))}"'


def _render_steps_body(
    pages: Any,
    fields: list[tuple[str, dict[str, Any], bool]],
    field_by_name: dict[str, tuple[dict[str, Any], bool]],
    values: dict[str, Any],
    options_map: dict[str, Any],
    submit_label: str | None,
    field_wording: dict[str, Any],
) -> str:
    """The form's ``<section class="step">`` body: one step per page (or one for a page-less form).

    Each input page renders its fields in the shared drawing order (each ``timeOf`` time right after
    its date) — a drawable ``rangeStart`` pair as one grouped fieldset at the START's position,
    holding the ends' times (the END's and the grouped times' own rows skipped), everything else
    as its own row — plus its display blocks and, in a ``data-button-label`` attribute, its
    primary-button label; a review page carries the generic readback target and no inputs. A
    page-less form draws ONE step whose terminal button reads ``submit_label``
    (``wording.form.submit``) when set. A select field's empty-state prompt reads the caller's
    ``wording.form.fields[name].choice_label`` and every control carries the rest of its field
    words (``field_wording``, see :func:`_render_field`).
    """
    properties = {name: prop for name, (prop, _required) in field_by_name.items()}

    def control(name: str) -> str:
        prop, is_required = field_by_name[name]
        return _render_field(
            name,
            prop,
            is_required,
            value=values.get(name),
            options=options_map.get(name),
            words=_field_words(field_wording, name),
            properties=properties,
        )

    def render_names(names: list[str]) -> str:
        ordered = drawing_order(names, properties)
        merged = _drawable_date_ranges(ordered, field_by_name)
        skipped = set(merged.values())
        rendered: list[str] = []
        for name in ordered:
            if name in skipped:
                continue
            if name in merged:
                times = _page_times(name, merged[name], ordered, properties)
                skipped.update(times.values())
                drawn_times = {date_name: control(time_name) for date_name, time_name in times.items()}
                group = _date_range_group(name, merged[name], field_by_name, values, field_wording, drawn_times)
                rendered.append(_field_row(name, field_by_name[name][0], group))
            else:
                rendered.append(_field_row(name, field_by_name[name][0], control(name)))
        return "\n".join(rendered)

    if not pages:
        rows = render_names([name for name, _prop, _req in fields])
        # The page-less form draws one step whose terminal (submit) button reads the caller's
        # ``wording.form.submit`` when set; the constant script's ``show`` draws it, else "Submit".
        bl_attr = f' data-button-label="{_attr(submit_label)}"' if submit_label else ""
        return f'<section class="step" data-step="0"{bl_attr}>\n{rows}\n</section>'
    steps_html = []
    for index, page in enumerate(pages):
        raw_title = str(page.get("title") or "")
        display_html = "\n".join(_render_display(block) for block in (page.get("display") or []))
        hidden = "" if index == 0 else " hidden"
        # The page's primary button (the one that advances or submits) reads its ``button_label``
        # when set; the submit script's ``show`` draws it on the shown step, else "Next"/"Submit".
        button_label = page.get("button_label")
        bl_attr = f' data-button-label="{_attr(button_label)}"' if button_label else ""
        if page.get("kind") == "review":
            # A review page carries no inputs: a generic readback (filled by the submit script from
            # the entered values) plus any display blocks and the confirm footer.
            inner = f"{display_html}\n<dl data-readback></dl>"
        else:
            inner = f"{display_html}\n{render_names(page['fields'])}"
        steps_html.append(
            f'<section class="step" data-step="{index}" data-page-title="{_attr(raw_title)}"{bl_attr}{hidden}>\n'
            f'<h2 tabindex="-1">{html.escape(raw_title)}</h2>\n{inner}\n</section>'
        )
    return "\n".join(steps_html)


def _wording_text_attrs(wording: dict[str, Any]) -> str:
    # The caller's form-page sentences as escaped ``data-*`` attributes the constant submit script
    # reads — the refusal sentence (``wording.answer.rejected``), the could-not-update sentence
    # (``wording.form.update_failed``), the page's own validation/error sentences
    # (``wording.answer_page.form.*``) and the submitted-state copy
    # (``wording.answer_page.submitted``). Each is written ONLY when set, so an absent member leaves
    # the script on its own default word. No caller string is interpolated into markup — only into a
    # single escaped attribute value the script copies to ``textContent``.
    answer = wording.get("answer") or {}
    form = wording.get("form") or {}
    answer_page = wording.get("answer_page") or {}
    form_page = answer_page.get("form") or {}
    submitted = answer_page.get("submitted") or {}
    parts: list[str] = []
    if answer.get("rejected"):
        parts.append(f' data-text-rejected="{_attr(answer["rejected"])}"')
    if form.get("update_failed"):
        parts.append(f' data-text-update_failed="{_attr(form["update_failed"])}"')
    parts.extend(
        f' data-text-{member}="{_attr(form_page[member])}"'
        for member in ("failed", "network_error", "choose_one", "date_unavailable")
        if form_page.get(member)
    )
    if submitted.get("heading"):
        parts.append(f' data-submitted-heading="{_attr(submitted["heading"])}"')
    if submitted.get("text"):
        parts.append(f' data-submitted-text="{_attr(submitted["text"])}"')
    return "".join(parts)


def _render_form_page(format_payload: dict[str, Any] | None) -> str:
    """Render the schema-driven HTML form for a channel-delivered form question.

    Uses the SAME subset walk (``channel_form_fields``) that ``ask`` enforces at ask time.
    Per-send ``data`` prefills known values and renders per-send option lists; ``pages`` split
    the fields into ordered steps (Back/Next/Submit, one visible at a time), each carrying its
    ordered display blocks, a REVIEW page showing a generic readback of the entered values, and
    conditional show/hide per field. The form carries ``novalidate``: its submit script runs every
    check itself (a missing required value shows the caller's ``required`` sentence when set, else
    the browser's own prompt), so a broken shape shows the field's sentence rather than the
    browser's words. A schema outside the subset raises ``_FormRenderError``; the GET door maps that
    to a loud 500 (the server-bug backstop for a record that bypassed ``ask``).
    """
    payload = format_payload or {}
    schema = payload.get("schema")
    try:
        fields = channel_form_fields(schema)
    except ValueError as exc:
        raise _FormRenderError(str(exc)) from exc
    reactions_attr = _reactions_attr(payload.get("reactions"))
    data = payload.get("data") or {}
    values = data.get("values") or {}
    options_map = data.get("options") or {}
    field_by_name = {name: (prop, is_required) for name, prop, is_required in fields}
    # The review readback lists the fields in the drawing order, each time after its date.
    titles = {name: str(prop.get("title") or name) for name, prop, _ in fields}
    labels = {name: titles[name] for name in drawing_order(list(titles), {name: prop for name, prop, _ in fields})}
    # The caller's per-send words ride the ``wording`` catalogue in the stored payload. Its ``form``
    # chapter carries the page's chrome (title/back/the page-less submit label), the per-field words
    # and the could-not-update sentence; its ``answer`` and ``answer_page`` chapters carry the other
    # sentences the constant submit script draws.
    wording = payload.get("wording") or {}
    form = wording.get("form") or {}
    body = _render_steps_body(
        payload.get("pages"), fields, field_by_name, values, options_map, form.get("submit"), form.get("fields") or {}
    )
    labels_attr = _attr(json.dumps(labels))
    form_title = form.get("title")
    heading = html.escape(str(form_title)) if form_title else "Respond"
    back_label = form.get("back")
    back_text = html.escape(str(back_label)) if back_label else "Back"
    text_attrs = _wording_text_attrs(wording)
    return (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{heading}</title>\n'
        "<style>body{font-family:system-ui,sans-serif;margin:3rem;max-width:40rem}"
        "label{display:block;margin:1rem 0}label.choice{display:block;margin:.3rem 0}"
        "input,select,textarea{font-size:1rem;margin-top:.3rem}"
        "small.choice-desc{display:block;font-size:.85rem;color:#666}"
        "fieldset{margin:1rem 0;border:1px solid #ccc;padding:.5rem 1rem}"
        "fieldset.chips{border:0;padding:0;margin:1rem 0}"
        "fieldset.chips label.chip{display:inline-block;margin:.3rem .4rem 0 0;padding:.35rem .8rem;"
        "border:1px solid #888;border-radius:1rem;cursor:pointer}"
        "fieldset.chips label.chip input{position:absolute;width:1px;height:1px;opacity:0}"
        "fieldset.chips label.chip:has(:checked){background:#0b57d0;color:#fff;border-color:#0b57d0}"
        "fieldset.chips label.chip:has(:checked) small.choice-desc{color:inherit}"
        "fieldset.daterange .range-end{display:inline-block;vertical-align:top;margin:0 1rem 0 0}"
        "fieldset.daterange .range-end label{margin:.3rem 0}"
        "small.hint{display:block;color:#555;margin-top:.2rem;font-size:.85rem}"
        "h2{font-size:1.1rem;margin-top:1.5rem}h3{font-size:1rem;margin:1rem 0 .3rem}"
        "dl{margin:1rem 0}dt{font-weight:600;margin-top:.6rem}dd{margin:0 0 .3rem}"
        "img{max-width:100%}"
        "button{font-size:1rem;padding:.6rem 1.4rem;margin-top:1rem;margin-right:.5rem}"
        "#err{color:#b00020;margin-top:1rem}.field-error{color:#b00020;margin:.3rem 0}</style></head><body>\n"
        f"<h1>{heading}</h1>\n"
        f'<form id="askform" novalidate data-labels="{labels_attr}"{reactions_attr}{text_attrs}>\n'
        f"{body}\n"
        '<div id="err" role="alert"></div>\n'
        '<div class="nav">\n'
        f'<button type="button" data-nav="back" hidden>{back_text}</button>\n'
        '<button type="button" data-nav="next" hidden>Next</button>\n'
        '<button type="submit" data-nav="submit">Submit</button>\n'
        "</div>\n"
        "</form>\n"
        f"{_FORM_SUBMIT_SCRIPT}"
        "</body></html>\n"
    )
