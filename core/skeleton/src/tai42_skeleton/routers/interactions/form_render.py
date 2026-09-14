"""Server-side rendering of a channel-delivered form question's schema into an
escaped HTML page."""

from __future__ import annotations

import html
from typing import Any

from tai42_skeleton.interactions.form_schema import channel_form_fields

from .pages import _FORM_SUBMIT_SCRIPT


class _FormRenderError(Exception):
    """Raised when a stored form question's schema cannot be rendered into a page —
    a schema outside the channel-deliverable subset (``form_schema``). ``ask_user``
    refuses such a schema before persisting, so a form record that reaches the GET
    door MUST render; failing to is a server bug (a record that bypassed
    ``ask_user``), so it surfaces as a loud 500 with a logged reason, never a blank
    or half-rendered page silently dropping fields."""


def _string_select(esc_name: str, req_attr: str, choices: list[tuple[str, str]], value: Any) -> str:
    # A ``<select>`` over ``(value, label)`` pairs: a leading blank option lets an
    # optional select stay empty and forces a required one to a real choice on submit;
    # the pair whose value matches the prefill is pre-selected.
    parts = ['<option value="">—</option>']
    for opt_value, opt_label in choices:
        selected = " selected" if value is not None and str(value) == opt_value else ""
        parts.append(
            f'<option value="{html.escape(opt_value, quote=True)}"{selected}>{html.escape(opt_label)}</option>'
        )
    return f'<select data-field="{esc_name}" data-kind="string"{req_attr}>' + "".join(parts) + "</select>"


def _render_field(
    name: str,
    prop: dict[str, Any],
    is_required: bool,
    *,
    value: Any = None,
    options: list[dict[str, Any]] | None = None,
) -> str:
    """Render one subset-validated schema property into an escaped form control:
    ``string`` (per-send ``options`` or ``enum`` -> ``<select>``, else text),
    ``boolean`` -> checkbox, ``integer``/``number`` -> number input. The property is
    pre-validated by ``channel_form_fields`` (the one subset definition), so its type
    is always a scalar and any ``enum`` is a non-empty list of strings; an unexpected
    type is a server bug and raises ``_FormRenderError``. ``value`` prefills the
    control; ``options`` (a per-send choice list, ``{"value", "label"?}`` each)
    REPLACES the schema ``enum`` for this send, showing labels and posting values.
    ``data-field``/``data-kind`` drive the submit script's typed collection."""
    esc_name = html.escape(name, quote=True)
    esc_label = html.escape(str(prop.get("title") or name))
    req_attr = " required" if is_required else ""
    ptype = prop.get("type")
    if ptype == "string":
        if options is not None:
            choices = [(str(option["value"]), str(option.get("label") or option["value"])) for option in options]
            control = _string_select(esc_name, req_attr, choices, value)
        elif prop.get("enum") is not None:
            control = _string_select(esc_name, req_attr, [(str(c), str(c)) for c in prop["enum"]], value)
        else:
            val_attr = f' value="{html.escape(str(value), quote=True)}"' if value is not None else ""
            control = f'<input data-field="{esc_name}" data-kind="string" type="text"{val_attr}{req_attr}>'
    elif ptype == "boolean":
        # A checkbox always submits a boolean (checked/unchecked), so the field is
        # always present — no ``required`` attribute, which would force it checked.
        checked = " checked" if value is True else ""
        control = f'<input data-field="{esc_name}" data-kind="boolean" type="checkbox"{checked}>'
    elif ptype in ("integer", "number"):
        step = ' step="1"' if ptype == "integer" else ' step="any"'
        val_attr = f' value="{html.escape(str(value), quote=True)}"' if value is not None else ""
        control = f'<input data-field="{esc_name}" data-kind="number" type="number"{step}{val_attr}{req_attr}>'
    else:
        raise _FormRenderError(f"form schema property {name!r} has unsupported type {ptype!r}")
    return f"<label>{esc_label}<br>{control}</label>"


def _render_form_page(format_payload: dict[str, Any] | None) -> str:
    """Render the schema-driven HTML form for a channel-delivered form question,
    over the SAME subset walk (``channel_form_fields``) that ``ask_user`` enforces
    at ask time. Per-send ``data`` prefills known values and renders per-send option
    lists; ``pages`` split the fields into ordered steps (Back/Next/Submit, one
    visible at a time), the answer being the union of every step's fields. A schema
    outside the subset raises ``_FormRenderError``; the GET door maps that to a loud
    500 (the server-bug backstop for a record that bypassed ``ask_user``)."""
    payload = format_payload or {}
    schema = payload.get("schema")
    try:
        fields = channel_form_fields(schema)
    except ValueError as exc:
        raise _FormRenderError(str(exc)) from exc
    data = payload.get("data") or {}
    values = data.get("values") or {}
    options_map = data.get("options") or {}
    field_by_name = {name: (prop, is_required) for name, prop, is_required in fields}

    def render_one(name: str) -> str:
        prop, is_required = field_by_name[name]
        return _render_field(name, prop, is_required, value=values.get(name), options=options_map.get(name))

    pages = payload.get("pages")
    if pages:
        # One step per page; the property order within a step follows the page's declared
        # ``fields``. Every declared property is covered (validated at ask time), so no
        # field is silently dropped.
        steps_html = []
        for index, page in enumerate(pages):
            title = html.escape(str(page.get("title") or ""))
            rows = "\n".join(render_one(name) for name in page["fields"])
            hidden = "" if index == 0 else " hidden"
            steps_html.append(
                f'<section class="step" data-step="{index}"{hidden}>\n'
                f'<h2 tabindex="-1">{title}</h2>\n{rows}\n</section>'
            )
        body = "\n".join(steps_html)
    else:
        rows = "\n".join(render_one(name) for name, _prop, _req in fields)
        body = f'<section class="step" data-step="0">\n{rows}\n</section>'
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><title>Respond</title>\n'
        "<style>body{font-family:system-ui,sans-serif;margin:3rem;max-width:40rem}"
        "label{display:block;margin:1rem 0}input,select{font-size:1rem;margin-top:.3rem}"
        "h2{font-size:1.1rem;margin-top:1.5rem}"
        "button{font-size:1rem;padding:.6rem 1.4rem;margin-top:1rem;margin-right:.5rem}"
        "#err{color:#b00020;margin-top:1rem}</style></head><body>\n"
        "<h1>Respond</h1>\n"
        '<form id="askform">\n'
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
