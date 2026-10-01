"""Interactions callback GET door: server-side rendering of a channel-delivered form
question's schema into an escaped HTML form."""

from __future__ import annotations

from tai42_skeleton.routers import interactions as router
from tai42_skeleton.routers.interactions.form_render import _reactions_attr

from ._harness import _seed_form, _seed_form_payload, _seed_reacting_form, make_request


async def test_get_form_ticket_renders_schema_form(wired):
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "title": "Full <name> & id"},
            "color": {"type": "string", "enum": ["red", "blue"]},
            "size": {"type": "string", "enum": ["xs", "s", "m", "l", "xl", "xxl"]},
            "agree": {"type": "boolean"},
            "count": {"type": "integer"},
        },
    }
    await _seed_form(wired, schema=schema)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # The label is HTML-escaped — the raw angle brackets/ampersand never inject markup.
    assert "Full &lt;name&gt; &amp; id" in body
    assert "<name>" not in body
    # Each control is present and typed for the submit script; ``required`` rides
    # the required field only.
    assert 'data-field="name" data-kind="string" type="text" required' in body
    # A SHORT enum (<= the radio threshold) renders a radio group, one input per choice.
    assert 'type="radio" name="color" data-field="color" data-kind="string" value="red"' in body
    assert 'type="radio" name="color" data-field="color" data-kind="string" value="blue"' in body
    # A LONG enum (above the threshold) renders a dropdown instead.
    assert '<select data-field="size" data-kind="string"' in body
    assert '<option value="xl">xl</option>' in body
    assert 'data-field="agree" data-kind="boolean" type="checkbox"' in body
    assert 'data-field="count" data-kind="number" type="number"' in body
    # The form-page CSP widens only to a constant inline script + same-origin fetch.
    csp = resp.headers["content-security-policy"]
    assert "script-src 'unsafe-inline'" in csp
    assert "connect-src 'self'" in csp
    assert resp.headers["cache-control"] == "no-store"


async def test_get_form_ticket_renders_prefill_and_per_send_options(wired):
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "color": {"type": "string", "enum": ["red", "blue"]},
            "count": {"type": "integer"},
            "agree": {"type": "boolean"},
        },
    }
    data = {
        "values": {"name": "Al", "color": "green", "count": 3, "agree": True},
        "options": {"color": [{"value": "green", "label": "Green"}, {"value": "amber", "label": "Amber"}]},
    }
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # Prefilled text/number values ride the control's ``value`` attribute.
    assert 'data-field="name" data-kind="string" type="text" value="Al"' in body
    assert 'data-field="count" data-kind="number" type="number" step="1" value="3"' in body
    # A prefilled checkbox is checked.
    assert 'data-field="agree" data-kind="boolean" type="checkbox" checked' in body
    # Per-send options REPLACE the schema enum: two choices render a radio group, labels
    # shown and values posted, the prefilled choice pre-checked.
    assert 'data-field="color" data-kind="string" value="green" checked> Green' in body
    assert 'data-field="color" data-kind="string" value="amber"> Amber' in body
    # The replaced enum values never render.
    assert ">red<" not in body
    assert ">blue<" not in body


async def test_get_form_ticket_renders_format_controls(wired):
    schema = {
        "type": "object",
        "properties": {
            "day": {"type": "string", "format": "date"},
            "at": {"type": "string", "format": "time"},
            "stamp": {"type": "string", "format": "date-time"},
        },
    }
    data = {"values": {"day": "2024-01-15"}}
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # ``format: date`` renders the native date control with the prefilled ISO value.
    assert 'data-field="day" data-kind="string" type="date" value="2024-01-15"' in body
    # ``time`` and ``date-time`` render as text — no native control emits a conforming value.
    assert 'data-field="at" data-kind="string" type="text"' in body
    assert 'data-field="stamp" data-kind="string" type="text"' in body


async def test_get_form_ticket_renders_pages_as_steps(wired):
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
    }
    pages = [{"title": "Who", "fields": ["name"]}, {"title": "How many", "fields": ["count"]}]
    await _seed_form_payload(wired, format_payload={"schema": schema, "pages": pages})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # One step section per page; the first is visible, the rest hidden until Next. Each
    # section carries its raw page title so the submit script can match a page_advanced
    # reaction trigger against it.
    assert '<section class="step" data-step="0" data-page-title="Who">' in body
    assert '<section class="step" data-step="1" data-page-title="How many" hidden>' in body
    # Each step's heading is focusable (``tabindex="-1"``) so the script can move focus
    # to it when a step becomes visible; the script's ``show(i)`` targets ``h2`` as its
    # fallback focus target after the step's first control.
    assert '<h2 tabindex="-1">Who</h2>' in body
    assert '<h2 tabindex="-1">How many</h2>' in body
    assert "steps[i].querySelector('h2')" in body
    assert "focusTarget.focus()" in body
    # Back/Next/Submit nav is present for the step script to wire.
    assert 'data-nav="back"' in body
    assert 'data-nav="next"' in body
    assert 'data-nav="submit"' in body


async def test_get_form_ticket_renders_multi_select(wired):
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["red", "blue", "green"]}}},
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # Array-of-strings renders a checkbox group — each box contributes to the submitted list.
    assert 'type="checkbox" data-field="tags" data-kind="array" value="red"' in body
    assert 'type="checkbox" data-field="tags" data-kind="array" value="green"' in body


async def test_get_form_ticket_renders_date_bounds(wired):
    schema = {
        "type": "object",
        "properties": {
            "d": {
                "type": "string",
                "format": "date",
                "minDate": "2024-01-01",
                "maxDate": "2024-12-31",
                "unavailableDates": ["sunday"],
            },
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert 'type="date" min="2024-01-01" max="2024-12-31"' in body
    assert "data-unavailable=" in body


async def test_get_form_ticket_renders_a_date_range_as_two_inputs(wired):
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 1, "maxDays": 14},
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # TWO ordinary date inputs — never one combined control, never recombined.
    assert 'data-field="start" data-kind="string" type="date"' in body
    assert 'data-field="end" data-kind="string" type="date"' in body


async def test_get_form_ticket_renders_display_blocks_and_review(wired):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    pages = [
        {
            "title": "Who",
            "fields": ["name"],
            "display": [
                {"kind": "heading", "text": "Your details"},
                {"kind": "body", "text": "Tell us who you are."},
                {"kind": "image", "src": "https://cdn.example/x.png", "alt": "a logo"},
            ],
        },
        {"title": "Review", "kind": "review", "fields": [], "display": [{"kind": "body", "text": "Confirm?"}]},
    ]
    await _seed_form_payload(wired, format_payload={"schema": schema, "pages": pages})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert "<h3>Your details</h3>" in body
    assert '<p class="display-body">Tell us who you are.</p>' in body
    assert '<img src="https://cdn.example/x.png" alt="a logo">' in body
    # The review step carries the generic readback target and no input field.
    assert "<dl data-readback></dl>" in body
    # The readback is driven from the labels embedded on the form.
    assert "data-labels=" in body


async def test_get_form_ticket_renders_conditional_field(wired):
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "detail": {"type": "string", "visibleWhen": {"field": "mode", "equals": "a"}},
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # The conditional field's row carries its predicate for the client show/hide.
    assert 'data-field-row="detail" data-visible-when=' in body
    assert "mode" in body


async def test_get_form_ticket_renders_a_reacting_form_with_the_reaction_round_trip(wired):
    # A reacting form carries its triggers on the form (data-reactions), and the submit
    # script posts the on-change round-trip to the /react sibling of THIS callback URL and
    # applies the returned update.
    schema = {
        "type": "object",
        "properties": {"colour": {"type": "string", "enum": ["r", "g"]}, "size": {"type": "string", "enum": ["s"]}},
    }
    reactions = {"field_changed": ["colour"], "page_advanced": [], "submitted": True, "choices": ["size"]}
    await _seed_reacting_form(wired, schema=schema, reactions=reactions)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # The triggers ride the form; the script posts the reaction to the /react sibling door.
    assert "data-reactions=" in body
    assert "&quot;field_changed&quot;: [&quot;colour&quot;]" in body
    assert "&quot;submitted&quot;: true" in body
    # ``choices`` is NOT threaded into the trigger attribute (it governs submit-time
    # membership on the server, not when the client fires a reaction).
    assert "&quot;choices&quot;" not in body
    assert "var reactUrl = window.location.pathname + '/react';" in body
    assert "{ event: event, values: collect(true) }" in body
    assert "data.data.update" in body
    # The update is applied: values/options/errors/display all have an apply path.
    assert "function applyUpdate(update)" in body
    assert "rebuildChoices(n, update.options[n])" in body
    assert "function applyErrors(errors)" in body
    assert "function applyDisplay(display)" in body
    # The field_changed and submitted events are wired.
    assert "kind: 'field_changed', field: name" in body
    assert "reactions.submitted" in body


async def test_get_form_ticket_static_form_carries_no_reaction_wiring(wired):
    # A form with no reactions declared carries no data-reactions attribute; the reaction
    # paths in the constant script stay dormant (reactions parses to null).
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert "data-reactions=" not in body
    # The constant script still defines the (dormant) reaction machinery.
    assert "var reactUrl = window.location.pathname + '/react';" in body


def test_reactions_attr_is_empty_for_a_non_reacting_or_triggerless_block():
    # The defensive backstop: the request model forbids a reactions block with no trigger (or
    # without a reaction_tool), but the renderer never emits a data-reactions attribute for a
    # missing, non-dict, or trigger-less block — only a real reacting form carries one.
    assert _reactions_attr(None) == ""
    assert _reactions_attr("nope") == ""
    assert _reactions_attr({"field_changed": [], "page_advanced": [], "submitted": False}) == ""


def test_reactions_attr_carries_only_the_triggers_not_choices():
    # The on-change triggers ride the attribute; choices (a submit-time server concern) does not.
    attr = _reactions_attr({"field_changed": ["a"], "page_advanced": ["P"], "submitted": True, "choices": ["a"]})
    assert "field_changed" in attr
    assert "page_advanced" in attr
    assert "submitted" in attr
    assert "choices" not in attr
