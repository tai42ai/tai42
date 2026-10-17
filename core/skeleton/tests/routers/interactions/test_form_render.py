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


async def test_get_form_ticket_renders_field_and_option_second_lines(wired):
    # A field's schema ``description`` draws as a <small class="hint"> second line; a per-send
    # option's ``description`` draws as a <small class="choice-desc"> under a radio/checkbox
    # label and folds onto the label of a <select> row (which has no second-line slot). A blank
    # description is absent.
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "title": "Your note", "description": "Keep it short"},
            "blank": {"type": "string", "title": "Blank", "description": "   "},
            "tier": {"type": "string", "enum": ["a", "b"]},
            "many": {"type": "string", "enum": ["a", "b", "c", "d", "e", "f"]},
            "extras": {"type": "array", "items": {"type": "string"}},
        },
    }
    data = {
        "options": {
            "tier": [
                {"value": "a", "label": "Standard", "description": "2 hours, 50"},
                {"value": "b", "label": "Express", "description": "1 hour, 90"},
            ],
            "many": [
                {"value": "a", "label": "A", "description": "first"},
                {"value": "b", "label": "B"},
                {"value": "c", "label": "C"},
                {"value": "d", "label": "D"},
                {"value": "e", "label": "E"},
                {"value": "f", "label": "F"},
            ],
            "extras": [{"value": "x", "label": "Extra", "description": "adds 10"}],
        }
    }
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # A field's description is a hint between the label and the control.
    assert '<small class="hint">Keep it short</small>' in body
    # A blank description draws no hint.
    assert "Blank<br><small" not in body
    # A radio option (<= threshold) carries its description as a <small> second line.
    assert '> Standard<small class="choice-desc">2 hours, 50</small></label>' in body
    assert '> Express<small class="choice-desc">1 hour, 90</small></label>' in body
    # A long enum renders a <select>; the description folds onto the option text (no second-line slot).
    assert ">A — first</option>" in body
    assert ">B</option>" in body
    # A checkbox (array) option carries its description as a <small> second line.
    assert '> Extra<small class="choice-desc">adds 10</small></label>' in body


async def test_get_form_ticket_renders_second_lines_on_chips_textarea_and_date_range(wired):
    # A chip carries its option's description as a <small> second line inside the pill; a chips
    # group and a text area draw the field's description as a hint under the label; a date-range
    # pair draws each end's description under that end's own label.
    schema = {
        "type": "object",
        "properties": {
            "colour": {"type": "string", "enum": ["r", "b"], "control": "chips", "description": "Tap one"},
            "notes": {"type": "string", "control": "textarea", "description": "A few lines"},
            "start": {"type": "string", "format": "date", "title": "From", "description": "First day"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "To"},
        },
    }
    data = {"options": {"colour": [{"value": "r", "label": "Red", "description": "warm"}, {"value": "b"}]}}
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    assert '<fieldset class="chips"><legend>colour</legend><small class="hint">Tap one</small>' in body
    assert '> Red<small class="choice-desc">warm</small></label>' in body
    assert 'value="b"> b</label>' in body
    assert '<label>notes<br><small class="hint">A few lines</small><textarea' in body
    assert '<label>From<br><small class="hint">First day</small><input data-field="start"' in body
    assert '<label>To<br><input data-field="end"' in body


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


async def test_get_form_ticket_renders_input_mode_controls(wired):
    schema = {
        "type": "object",
        "properties": {
            "plain": {"type": "string", "inputMode": "text"},
            "num": {"type": "string", "inputMode": "numeric"},
            "dec": {"type": "string", "inputMode": "decimal"},
            "phone": {"type": "string", "inputMode": "tel"},
            "mail": {"type": "string", "inputMode": "email"},
            "link": {"type": "string", "inputMode": "url"},
        },
    }
    data = {"values": {"mail": "a@b.com"}}
    await _seed_form_payload(wired, format_payload={"schema": schema, "data": data})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    # Each kind picks its control; ``numeric``/``decimal`` stay ``type="text"`` with an
    # ``inputmode`` hint so the value stays the typed string. ``data-kind="string"`` is kept
    # for every kind so the submit script posts the string.
    assert 'data-field="plain" data-kind="string" type="text"' in body
    assert 'data-field="num" data-kind="string" type="text" inputmode="numeric"' in body
    assert 'data-field="dec" data-kind="string" type="text" inputmode="decimal"' in body
    assert 'data-field="phone" data-kind="string" type="tel"' in body
    assert 'data-field="mail" data-kind="string" type="email"' in body
    assert 'data-field="link" data-kind="string" type="url"' in body
    # Prefill rides the value attribute whatever the keyboard.
    assert 'data-field="mail" data-kind="string" type="email" value="a@b.com"' in body


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


async def test_get_form_ticket_groups_a_drawable_date_range(wired):
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date", "title": "From"},
            "end": {
                "type": "string",
                "format": "date",
                "rangeStart": "start",
                "minDays": 1,
                "maxDays": 14,
                "title": "To",
            },
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # A drawable pair (same page, both format date, same visibleWhen, no sibling depends on an
    # end) is ONE fieldset grouping the two date inputs, labelled by both ends' titles.
    assert '<fieldset class="daterange"><legend>From \u2013 To</legend>' in body
    assert 'data-field="start" data-kind="string" type="date"' in body
    assert 'data-field="end" data-kind="string" type="date"' in body
    # The pair is one field row (the END's own row is not drawn separately).
    assert 'data-field-row="start"' in body
    assert 'data-field-row="end"' not in body
    # The answer is still the two date strings — nothing is recombined.
    assert body.count('type="date"') == 2


async def test_get_form_ticket_draws_a_date_range_as_two_fields_when_a_sibling_depends_on_an_end(wired):
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date", "title": "From"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "To"},
            # A sibling whose visibleWhen names the END dissolves the merge: the pair stays two
            # date fields so the sibling's conditional still references a real control.
            "note": {"type": "string", "visibleWhen": {"field": "end", "equals": "2024-01-01"}},
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert '<fieldset class="daterange">' not in body
    assert 'data-field-row="start"' in body
    assert 'data-field-row="end"' in body
    assert 'data-field="start" data-kind="string" type="date"' in body
    assert 'data-field="end" data-kind="string" type="date"' in body


async def test_get_form_ticket_renders_chips_for_a_string_enum(wired):
    schema = {
        "type": "object",
        "properties": {"colour": {"type": "string", "enum": ["red", "blue"], "control": "chips"}},
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # A chip grid is a radio group inside a pill-styled fieldset; each pill keeps the radio
    # markup the submit script reads (name/data-field/data-kind="string").
    assert '<fieldset class="chips">' in body
    assert '<label class="choice chip"><input type="radio" name="colour" data-field="colour" ' in body
    assert 'data-kind="string" value="red"' in body
    # The page CSS hides the radio and styles the label as a pill.
    assert "fieldset.chips label.chip:has(:checked)" in body


async def test_get_form_ticket_renders_chips_for_an_array_of_strings(wired):
    schema = {
        "type": "object",
        "required": ["tags"],
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ["a", "b", "c"]},
                "control": "chips",
            }
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # Several chips selected -> checkboxes, data-kind="array"; the required group still carries
    # the data-array-required marker the submit script checks.
    assert '<fieldset class="chips" data-array-required="1">' in body
    assert '<label class="choice chip"><input type="checkbox" data-field="tags" data-kind="array" value="a"' in body


async def test_get_form_ticket_renders_a_textarea_with_its_maximum(wired):
    schema = {
        "type": "object",
        "properties": {
            "notes": {"type": "string", "control": "textarea", "maxLength": 500, "minLength": 10},
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # A multi-line control with its native length bounds and the maximum shown as a hint.
    assert '<textarea data-field="notes" data-kind="string" rows="4" maxlength="500" minlength="10">' in body
    assert '<small class="hint">up to 500 characters</small>' in body


async def test_get_form_ticket_draws_the_length_bounds_on_a_single_line_input(wired):
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "maxLength": 12, "minLength": 3},
        },
    }
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # The length bound is drawn on the plain single-line input too, with the maximum shown.
    assert '<input data-field="code" data-kind="string" type="text" maxlength="12" minlength="3"' in body
    assert '<small class="hint">up to 12 characters</small>' in body


async def test_get_form_ticket_draws_the_page_button_label_and_chrome(wired):
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "count": {"type": "integer"}}}
    pages = [
        {"title": "Who", "fields": ["name"], "button_label": "Continue"},
        {"title": "How many", "fields": ["count"], "button_label": "Send request"},
    ]
    payload = {
        "schema": schema,
        "pages": pages,
        "wording": {"form": {"title": "Your <details>", "back": "Go back"}},
    }
    await _seed_form_payload(wired, format_payload=payload)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # Each step carries its primary-button label for the submit script to draw.
    assert '<section class="step" data-step="0" data-page-title="Who" data-button-label="Continue">' in body
    assert 'data-button-label="Send request"' in body
    assert "next.textContent = (!last && bl) ? bl : 'Next';" in body
    assert "submit.textContent = (last && bl) ? bl : 'Submit';" in body
    # The form title heads the page (escaped), and the Back button reads the caller's word.
    assert "<title>Your &lt;details&gt;</title>" in body
    assert "<h1>Your &lt;details&gt;</h1>" in body
    assert '<button type="button" data-nav="back" hidden>Go back</button>' in body


async def test_get_form_ticket_chrome_defaults_when_absent(wired):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # No wording.form -> the surface's own words.
    assert "<title>Respond</title>" in body
    assert "<h1>Respond</h1>" in body
    assert '<button type="button" data-nav="back" hidden>Back</button>' in body


async def test_get_form_ticket_draws_the_page_less_submit_label(wired):
    # A page-less form's terminal button reads wording.form.submit on the single step.
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    payload = {"schema": schema, "wording": {"form": {"submit": "Send it"}}}
    await _seed_form_payload(wired, format_payload=payload)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert '<section class="step" data-step="0" data-button-label="Send it">' in body


async def test_get_form_ticket_draws_the_caller_choice_label_on_a_select(wired):
    # A select field's empty-state prompt reads wording.form.fields[name].choice_label, escaped;
    # radio groups (short enums) draw none, and an absent label keeps the dash.
    schema = {
        "type": "object",
        "properties": {
            "size": {"type": "string", "enum": ["xs", "s", "m", "l", "xl", "xxl"]},
            "color": {"type": "string", "enum": ["red", "blue"]},
        },
    }
    payload = {"schema": schema, "wording": {"form": {"fields": {"size": {"choice_label": "Pick a <size>"}}}}}
    await _seed_form_payload(wired, format_payload=payload)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # The select's leading blank option carries the caller's label, HTML-escaped.
    assert '<option value="">Pick a &lt;size&gt;</option>' in body
    assert "Pick a <size>" not in body
    # The radio-rendered field (and any unlabelled select) is untouched — no placeholder leaks to it.
    assert 'type="radio" name="color"' in body


async def test_get_form_ticket_select_without_a_choice_label_keeps_the_dash(wired):
    schema = {"type": "object", "properties": {"size": {"type": "string", "enum": ["xs", "s", "m", "l", "xl", "xxl"]}}}
    await _seed_form_payload(wired, format_payload={"schema": schema})
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert '<option value="">—</option>' in body


async def test_get_form_ticket_draws_the_wording_text_attributes(wired):
    # The caller's form-page sentences, refusal sentence and submitted copy ride escaped data-*
    # attributes the constant script reads; each is written only when set.
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    payload = {
        "schema": schema,
        "wording": {
            "answer": {"rejected": "No vale ({reason})."},
            "answer_page": {
                "form": {"failed": "Fallo al enviar.", "choose_one": "Elige una."},
                "submitted": {"heading": "Enviado", "text": "Gracias"},
            },
        },
    }
    await _seed_form_payload(wired, format_payload=payload)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    assert 'data-text-rejected="No vale ({reason})."' in body
    assert 'data-text-failed="Fallo al enviar."' in body
    assert 'data-text-choose_one="Elige una."' in body
    assert 'data-submitted-heading="Enviado"' in body
    assert 'data-submitted-text="Gracias"' in body
    # Unset members draw no attribute.
    assert "data-text-network_error" not in body
    assert "data-text-date_unavailable" not in body


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
    # The page policy admits the image sources a display block draws from: a remote https
    # url and the deployment's own same-origin served-media reference.
    directives = [directive.strip() for directive in resp.headers["content-security-policy"].split(";")]
    assert "img-src 'self' https:" in directives
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


async def test_reaction_choice_rebuild_draws_option_second_lines_and_keeps_the_select_placeholder(wired):
    # A reaction that replaces a field's choices rebuilds the control client-side, drawn as the
    # server draws it: a <select> keeps its existing empty-state text (the caller's choice_label,
    # else the dash) and folds an option's second line onto the option text after " — "; a
    # radio/checkbox/chip label gets the ``choice-desc`` second line.
    schema = {"type": "object", "properties": {"colour": {"type": "string", "enum": ["r", "g"]}}}
    reactions = {"field_changed": ["colour"], "page_advanced": [], "submitted": False, "choices": []}
    await _seed_reacting_form(wired, schema=schema, reactions=reactions)
    resp = await router.callback(make_request("GET", path_params={"ticket": "TKT"}))
    body = bytes(resp.body).decode()
    # The second line counts only when a non-blank string — the server's rule.
    assert "o.description.trim() !== '') ? o.description : null;" in body
    # The select keeps the placeholder it was drawn with instead of writing a fresh dash.
    assert "first.options[0].textContent : '\\u2014';" in body
    assert "blank.value = ''; blank.textContent = blankText;" in body
    assert "blank.textContent = '\\u2014';" not in body
    # A select option folds its second line onto the option text.
    assert "optionLabel(o) : optionLabel(o) + ' \\u2014 ' + second;" in body
    # A radio/checkbox/chip label carries its second line as a ``choice-desc`` small.
    assert "small.className = 'choice-desc';" in body
    assert "small.textContent = second;" in body


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
