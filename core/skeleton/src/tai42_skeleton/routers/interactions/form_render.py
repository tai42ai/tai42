"""Server-side rendering of a channel-delivered form question's schema into an escaped HTML page.

The one HTML fallback surface a channel-delivered form is answered on. It renders every field
kind the channel subset admits — scalars, a choice as a radio group (short lists) or a select
(long lists), a multiple-choice array as a checkbox group, a date with its native min/max bounds
— plus ordered display blocks (heading/body/image), a review/summary step, and conditional
show/hide driven by each field's ``visibleWhen`` predicate. Where a surface cannot draw a thing
it DEGRADES by a documented rule, never silently: an image with no static source shows its alt
text; arbitrary unavailable days cannot be disabled on a native date input, so the client best-
effort-validates them at submit and the server enforces them regardless. A date RANGE is simply
its two ordinary date fields (never one combined control, never recombined).
"""

from __future__ import annotations

import html
import json
from typing import Any

from tai42_skeleton.interactions.form_schema import RADIO_OPTION_THRESHOLD, channel_form_fields

from .pages import _FORM_SUBMIT_SCRIPT


class _FormRenderError(Exception):
    """Raised when a stored form question's schema cannot be rendered into a page.

    The schema is outside the channel-deliverable subset (``form_schema``). ``ask``
    refuses such a schema before persisting, so a form record that reaches the GET
    door MUST render; failing to is a server bug (a record that bypassed
    ``ask``), so it surfaces as a loud 500 with a logged reason, never a blank
    or half-rendered page silently dropping fields.
    """


def _attr(value: Any) -> str:
    # A double-quoted attribute value, HTML-escaped (quotes included).
    return html.escape(str(value), quote=True)


def _string_choices(prop: dict[str, Any], options: list[dict[str, Any]] | None) -> list[tuple[str, str]] | None:
    # The ``(value, label)`` choice pairs for a string property: the per-send option list when
    # given (it replaces the enum for this send), else the property's own ``enum``, else None
    # (a free-text string).
    if options is not None:
        return [(str(option["value"]), str(option.get("label") or option["value"])) for option in options]
    enum = prop.get("enum")
    if enum is not None:
        return [(str(choice), str(choice)) for choice in enum]
    return None


def _array_choices(prop: dict[str, Any], options: list[dict[str, Any]] | None) -> list[tuple[str, str]] | None:
    # The choice pairs for an array-of-strings property: the per-send option list, else the
    # string ``items`` enum, else None (a free list the human types one entry per line).
    if options is not None:
        return [(str(option["value"]), str(option.get("label") or option["value"])) for option in options]
    items = prop.get("items")
    enum = items.get("enum") if isinstance(items, dict) else None
    if isinstance(enum, list):
        return [(str(choice), str(choice)) for choice in enum]
    return None


def _string_select(esc_name: str, req_attr: str, choices: list[tuple[str, str]], value: Any) -> str:
    # A ``<select>`` over ``(value, label)`` pairs: a leading blank option lets an
    # optional select stay empty and forces a required one to a real choice on submit.
    parts = ['<option value="">—</option>']
    for opt_value, opt_label in choices:
        selected = " selected" if value is not None and str(value) == opt_value else ""
        parts.append(f'<option value="{_attr(opt_value)}"{selected}>{html.escape(opt_label)}</option>')
    return f'<select data-field="{esc_name}" data-kind="string"{req_attr}>' + "".join(parts) + "</select>"


def _string_radio(esc_name: str, req_attr: str, choices: list[tuple[str, str]], value: Any) -> str:
    # A radio group over ``(value, label)`` pairs — the short-list control. ``required`` on
    # every radio makes the browser demand one selection in the group.
    parts = []
    for opt_value, opt_label in choices:
        checked = " checked" if value is not None and str(value) == opt_value else ""
        parts.append(
            f'<label class="choice"><input type="radio" name="{esc_name}" data-field="{esc_name}" '
            f'data-kind="string" value="{_attr(opt_value)}"{checked}{req_attr}> {html.escape(opt_label)}</label>'
        )
    return "".join(parts)


def _checkbox_group(esc_name: str, choices: list[tuple[str, str]], value: Any) -> str:
    # A checkbox group for an array-of-strings (multiple-choice) field: each checked box
    # contributes its value to the submitted list.
    selected = {str(item) for item in value} if isinstance(value, list) else set()
    parts = []
    for opt_value, opt_label in choices:
        checked = " checked" if opt_value in selected else ""
        parts.append(
            f'<label class="choice"><input type="checkbox" data-field="{esc_name}" data-kind="array" '
            f'value="{_attr(opt_value)}"{checked}> {html.escape(opt_label)}</label>'
        )
    return "".join(parts)


def _date_input(esc_name: str, req_attr: str, prop: dict[str, Any], value: Any) -> str:
    # A native date input carrying its min/max bounds (the picker restricts the range) and,
    # as a best-effort client hint, the unavailable dates/weekdays the submit script checks —
    # the server enforces them regardless.
    bounds = ""
    if isinstance(prop.get("minDate"), str):
        bounds += f' min="{_attr(prop["minDate"])}"'
    if isinstance(prop.get("maxDate"), str):
        bounds += f' max="{_attr(prop["maxDate"])}"'
    unavailable = prop.get("unavailableDates")
    if isinstance(unavailable, list):
        bounds += f' data-unavailable="{_attr(json.dumps(unavailable))}"'
    val_attr = f' value="{_attr(value)}"' if value is not None else ""
    return f'<input data-field="{esc_name}" data-kind="string" type="date"{bounds}{val_attr}{req_attr}>'


def _labelled(esc_label: str, control: str, *, grouped: bool, group_attr: str = "") -> str:
    # Wrap a control in its label — a ``<fieldset>``/``<legend>`` for a radio/checkbox group
    # (one label cannot own several inputs), a ``<label>`` for a single control.
    if grouped:
        return f"<fieldset{group_attr}><legend>{esc_label}</legend>{control}</fieldset>"
    return f"<label>{esc_label}<br>{control}</label>"


def _render_field(
    name: str,
    prop: dict[str, Any],
    is_required: bool,
    *,
    value: Any = None,
    options: list[dict[str, Any]] | None = None,
) -> str:
    """Render one subset-validated schema property into an escaped form control.

    A choice (``enum`` or per-send ``options``) renders as a radio group at or below
    :data:`RADIO_OPTION_THRESHOLD` options, a ``<select>`` above it. An array of strings renders
    a checkbox group over its choices (or a one-entry-per-line text area when it declares none).
    A ``format: date`` string renders a native date input with its min/max bounds; ``time`` /
    ``date-time`` render as text (no native control emits their value shape). ``boolean`` ->
    checkbox, ``integer``/``number`` -> number input. The property is pre-validated by
    ``channel_form_fields``; an unexpected type is a server bug and raises ``_FormRenderError``.
    """
    esc_name = _attr(name)
    esc_label = html.escape(str(prop.get("title") or name))
    req_attr = " required" if is_required else ""
    ptype = prop.get("type")
    if ptype == "array":
        choices = _array_choices(prop, options)
        if choices is not None:
            group_attr = ' data-array-required="1"' if is_required else ""
            return _labelled(esc_label, _checkbox_group(esc_name, choices, value), grouped=True, group_attr=group_attr)
        # No declared choices: a free list, one entry per line (the submit script splits it).
        text = "\n".join(str(item) for item in value) if isinstance(value, list) else ""
        control = f'<textarea data-field="{esc_name}" data-kind="arraytext"{req_attr}>{html.escape(text)}</textarea>'
        return _labelled(esc_label, control, grouped=False)
    if ptype == "string":
        choices = _string_choices(prop, options)
        if choices is not None:
            if len(choices) <= RADIO_OPTION_THRESHOLD:
                return _labelled(esc_label, _string_radio(esc_name, req_attr, choices, value), grouped=True)
            return _labelled(esc_label, _string_select(esc_name, req_attr, choices, value), grouped=False)
        if prop.get("format") == "date":
            return _labelled(esc_label, _date_input(esc_name, req_attr, prop, value), grouped=False)
        val_attr = f' value="{_attr(value)}"' if value is not None else ""
        control = f'<input data-field="{esc_name}" data-kind="string" type="text"{val_attr}{req_attr}>'
        return _labelled(esc_label, control, grouped=False)
    if ptype == "boolean":
        # A checkbox always submits a boolean (checked/unchecked), so the field is always
        # present — no ``required`` attribute, which would force it checked.
        checked = " checked" if value is True else ""
        control = f'<input data-field="{esc_name}" data-kind="boolean" type="checkbox"{checked}>'
        return _labelled(esc_label, control, grouped=False)
    if ptype in ("integer", "number"):
        step = ' step="1"' if ptype == "integer" else ' step="any"'
        val_attr = f' value="{_attr(value)}"' if value is not None else ""
        control = f'<input data-field="{esc_name}" data-kind="number" type="number"{step}{val_attr}{req_attr}>'
        return _labelled(esc_label, control, grouped=False)
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


def _render_form_page(format_payload: dict[str, Any] | None) -> str:
    """Render the schema-driven HTML form for a channel-delivered form question.

    Uses the SAME subset walk (``channel_form_fields``) that ``ask`` enforces at ask time.
    Per-send ``data`` prefills known values and renders per-send option lists; ``pages`` split
    the fields into ordered steps (Back/Next/Submit, one visible at a time), each carrying its
    ordered display blocks, a REVIEW page showing a generic readback of the entered values, and
    conditional show/hide per field. A schema outside the subset raises ``_FormRenderError``; the
    GET door maps that to a loud 500 (the server-bug backstop for a record that bypassed ``ask``).
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
    labels = {name: str(prop.get("title") or name) for name, prop, _ in fields}

    def render_one(name: str) -> str:
        prop, is_required = field_by_name[name]
        inner = _render_field(name, prop, is_required, value=values.get(name), options=options_map.get(name))
        return _field_row(name, prop, inner)

    pages = payload.get("pages")
    if pages:
        steps_html = []
        for index, page in enumerate(pages):
            raw_title = str(page.get("title") or "")
            title = html.escape(raw_title)
            display_html = "\n".join(_render_display(block) for block in (page.get("display") or []))
            hidden = "" if index == 0 else " hidden"
            if page.get("kind") == "review":
                # A review page carries no inputs: a generic readback (filled by the submit
                # script from the entered values) plus any display blocks and the confirm footer.
                inner = f"{display_html}\n<dl data-readback></dl>"
            else:
                rows = "\n".join(render_one(name) for name in page["fields"])
                inner = f"{display_html}\n{rows}"
            steps_html.append(
                f'<section class="step" data-step="{index}" data-page-title="{_attr(raw_title)}"{hidden}>\n'
                f'<h2 tabindex="-1">{title}</h2>\n{inner}\n</section>'
            )
        body = "\n".join(steps_html)
    else:
        rows = "\n".join(render_one(name) for name, _prop, _req in fields)
        body = f'<section class="step" data-step="0">\n{rows}\n</section>'
    labels_attr = _attr(json.dumps(labels))
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><title>Respond</title>\n'
        "<style>body{font-family:system-ui,sans-serif;margin:3rem;max-width:40rem}"
        "label{display:block;margin:1rem 0}label.choice{display:block;margin:.3rem 0}"
        "input,select,textarea{font-size:1rem;margin-top:.3rem}"
        "fieldset{margin:1rem 0;border:1px solid #ccc;padding:.5rem 1rem}"
        "h2{font-size:1.1rem;margin-top:1.5rem}h3{font-size:1rem;margin:1rem 0 .3rem}"
        "dl{margin:1rem 0}dt{font-weight:600;margin-top:.6rem}dd{margin:0 0 .3rem}"
        "img{max-width:100%}"
        "button{font-size:1rem;padding:.6rem 1.4rem;margin-top:1rem;margin-right:.5rem}"
        "#err{color:#b00020;margin-top:1rem}.field-error{color:#b00020;margin:.3rem 0}</style></head><body>\n"
        "<h1>Respond</h1>\n"
        f'<form id="askform" data-labels="{labels_attr}"{reactions_attr}>\n'
        f"{body}\n"
        '<div id="err" role="alert"></div>\n'
        '<div class="nav">\n'
        '<button type="button" data-nav="back" hidden>Back</button>\n'
        '<button type="button" data-nav="next" hidden>Next</button>\n'
        '<button type="submit" data-nav="submit">Submit</button>\n'
        "</div>\n"
        "</form>\n"
        f"{_FORM_SUBMIT_SCRIPT}"
        "</body></html>\n"
    )
