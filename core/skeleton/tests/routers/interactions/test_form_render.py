"""Interactions callback GET door: server-side rendering of a channel-delivered form
question's schema into an escaped HTML form."""

from __future__ import annotations

from tai42_skeleton.routers import interactions as router

from ._harness import _seed_form, _seed_form_payload, make_request


async def test_get_form_ticket_renders_schema_form(wired):
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "title": "Full <name> & id"},
            "color": {"type": "string", "enum": ["red", "blue"]},
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
    assert 'data-field="color" data-kind="string"' in body
    assert "<select" in body
    assert '<option value="red">red</option>' in body
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
    # Per-send options REPLACE the schema enum: labels shown, values posted, prefill selected.
    assert '<option value="green" selected>Green</option>' in body
    assert '<option value="amber">Amber</option>' in body
    # The replaced enum values never render.
    assert ">red<" not in body
    assert ">blue<" not in body


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
    # One step section per page; the first is visible, the rest hidden until Next.
    assert '<section class="step" data-step="0">' in body
    assert '<section class="step" data-step="1" hidden>' in body
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
