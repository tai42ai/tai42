"""HTML pages and response-header sets for the unauthenticated interactions callback surface.

The confirm/done/awaiting pages interpolate only the caller's contract-validated per-send
``wording`` (headings, sentences and the confirm button, each escaped); absent, each shows the
platform's own word. The form page interpolates only its escaped schema/page-derived markup and
the caller's words through escaped ``data-*`` attributes its constant script reads. No
request-derived value (query, ticket, path) is interpolated on any page.
"""

from __future__ import annotations

import html

from tai42_contract.interactions.models import Wording

# no-store + nosniff ride EVERY callback response: capability-bearing URLs must
# never be cached, and the schema-mismatch 400 reflects attacker-influenced
# content that browsers reach via the confirm flow.
_BASE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
# HTML pages add the anti-injection headers for the platform's only
# unauthenticated HTML route.
_HTML_HEADERS = {
    **_BASE_HEADERS,
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# The form page needs its own inline submit script (a native form cannot build the
# typed ``{"answer": {...}}`` JSON body) plus a same-origin fetch back to this
# callback URL. The CSP widens only to a constant inline ``script-src`` and a
# ``connect-src``/``form-action`` pinned to ``'self'`` — no ``unsafe-eval`` — over the
# same locked-down ``default-src 'none'`` base. ``img-src`` admits the sources a
# display-block image draws from: a remote ``https:`` url and the deployment's own
# same-origin served-media reference (``http:`` stays excluded — mixed content); an
# image cannot execute script, so the script posture is unaffected.
_FORM_HTML_HEADERS = {
    **_BASE_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' https:; connect-src 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _answer_page_copy(wording: Wording | None, page: str) -> tuple[str | None, str | None]:
    # The caller's ``(heading, text)`` for one answer page off the per-send catalogue; each ``None``
    # when unset (the renderer then shows its own default). The record was contract-validated at the
    # ask door, so a set value is a non-blank string.
    if wording is None or wording.answer_page is None:
        return None, None
    copy = getattr(wording.answer_page, page)
    if copy is None:
        return None, None
    return copy.heading, copy.text


_PAGE_STYLE = "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}"


def _render_confirm_page(wording: Wording | None) -> str:
    """The confirm page served on GET for a ``confirm``/``external`` ask.

    Its ``<title>``/``<h1>`` read ``wording.answer_page.confirm.heading`` (else "Confirm"), its
    sentence ``.text`` (else "Click confirm to submit your response."), and its one button
    ``wording.answer.confirm`` (else "Confirm") — each a contract-validated string from the stored
    ask, interpolated escaped under the locked-down CSP. The empty-body POST to the SAME URL records
    the answer (``confirm`` -> True; ``external`` -> the query-param payload), so the form's empty
    action preserves the query string.
    """
    heading, body = _answer_page_copy(wording, "confirm")
    title = html.escape(heading) if heading else "Confirm"
    sentence = html.escape(body) if body else "Click confirm to submit your response."
    confirm = wording.answer.confirm if wording is not None and wording.answer is not None else None
    label = html.escape(confirm) if confirm else "Confirm"
    return (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{title}</title>\n'
        f"{_PAGE_STYLE}"
        "button{font-size:1rem;padding:.6rem 1.4rem}</style></head><body>\n"
        f"<h1>{title}</h1>\n"
        f"<p>{sentence}</p>\n"
        f'<form method="post"><button type="submit">{label}</button></form>\n'
        "</body></html>\n"
    )


def _render_done_page(wording: Wording | None) -> str:
    """The done page served on GET for an already-answered interaction of any format.

    Its ``<title>``/``<h1>`` read ``wording.answer_page.done.heading`` (else "Done") and its
    sentence ``.text`` (else "This interaction has already been answered."), escaped.
    """
    heading, body = _answer_page_copy(wording, "done")
    title = html.escape(heading) if heading else "Done"
    sentence = html.escape(body) if body else "This interaction has already been answered."
    return (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{title}</title>\n'
        f"{_PAGE_STYLE}</style></head><body>\n"
        f"<h1>{title}</h1>\n"
        f"<p>{sentence}</p>\n"
        "</body></html>\n"
    )


def _render_awaiting_page(wording: Wording | None) -> str:
    """The awaiting-reply page served on GET for a ticketed ``text``/``select`` question.

    A value answer is required, so the page presents no form — the confirm button's empty-body POST
    could never succeed here. ``wording.answer_page.awaiting.heading`` reads into BOTH the
    ``<title>`` and the ``<h1>`` when set; absent, the page keeps its own two words ("Awaiting
    reply" / "Awaiting your reply"). The sentence reads ``.text`` (else "Answer this question by
    replying on the channel where you received it."), escaped.
    """
    heading, body = _answer_page_copy(wording, "awaiting")
    title = html.escape(heading) if heading else "Awaiting reply"
    h1 = html.escape(heading) if heading else "Awaiting your reply"
    sentence = html.escape(body) if body else "Answer this question by replying on the channel where you received it."
    return (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{title}</title>\n'
        f"{_PAGE_STYLE}</style></head><body>\n"
        f"<h1>{h1}</h1>\n"
        f"<p>{sentence}</p>\n"
        "</body></html>\n"
    )


# The constant submit script for the form page. It drives the stepped pages (showing one
# ``.step`` section at a time, wiring Back/Next, revealing Submit only on the last step — a
# single-step form hides Back/Next), collects EVERY rendered field across all steps (coercing
# numbers, booleans, radio choices and multi-select / list fields, omitting empty optional
# fields), shows/hides each conditional field by its ``visibleWhen`` predicate (a hidden field
# is dropped from the answer), fills a review step's readback from the entered values, and
# best-effort-rejects an unavailable date before POSTing the union as ``{"answer": {...}}`` JSON
# to THIS same callback URL. Before a step advances or the form submits it checks every visible
# control's declared shape — the browser's own constraint states, the schema ``pattern`` and the
# platform's format regex (each an unanchored ``RegExp`` search), a range END's order and span —
# and pins the field's sentence (the caller's ``data-invalid``, else the platform's
# ``data-msg-<kind>``) under its row, keeping the visitor on the step. On 200 it swaps to a done
# state; on 400 it renders the door's own error text, pins it under the field the door names, and
# lets the visitor retry.
#
# When the form declares reactions (``data-reactions`` on the form), it also runs the on-change
# reaction round-trip: a change to a declared field, an advance from a declared page, or submit
# (when the form declares a submitted check) POSTs ``{"event": {kind, field?|page?}, "values":
# {...}}`` to the ``/react`` sibling of this callback URL (the ticket react door — the ticket
# stays in the address) and applies the returned form update — setting values, replacing an
# option-bearing field's choice list (drawn exactly as the server draws it: each option's second
# line as a ``choice-desc`` line under a radio/checkbox/chip label or folded onto a ``<select>``
# row, and a select's empty-state prompt kept), showing per-field errors, and filling display slots. A
# submitted reaction that returns errors (or fails) keeps the form open; only a clean one sends
# the answer. A reaction error surfaces loudly, never a stale/silent value. A static form carries
# no ``data-reactions`` and every reaction path is dormant. The script body is a constant — only
# the field markup above it is schema-derived (and escaped).
_FORM_SUBMIT_SCRIPT = """<script>
(function () {
  var form = document.getElementById('askform');
  var err = document.getElementById('err');
  var steps = form.querySelectorAll('.step');
  var rows = form.querySelectorAll('[data-visible-when]');
  var back = form.querySelector('[data-nav="back"]');
  var next = form.querySelector('[data-nav="next"]');
  var submit = form.querySelector('[data-nav="submit"]');
  var labels = {};
  try { labels = JSON.parse(form.getAttribute('data-labels') || '{}'); } catch (e) { labels = {}; }
  // A reacting form declares its triggers in data-reactions; a static form carries none
  // (null) and every reaction path below is dormant. The reaction round-trip posts to the
  // ticket react door — this page's own callback URL plus its /react sibling — keeping the
  // ticket in the address, exactly the capability that answers the form.
  var reactions = null;
  try { reactions = JSON.parse(form.getAttribute('data-reactions') || 'null'); } catch (e) { reactions = null; }
  var reactUrl = window.location.pathname + '/react';
  var WEEKDAYS = ['sunday','monday','tuesday','wednesday','thursday','friday','saturday'];
  var current = 0;
  // The page's own sentences: the caller's words ride escaped data-* attributes the renderer
  // writes only when set; absent, each reads the platform's own default below. The script stays a
  // byte constant — it reads attributes and writes textContent, never interpolating a caller string
  // into markup, never innerHTML from a caller string, never String.replace (split/join fills the
  // {reason} token so a $-sequence in a reason is never interpreted).
  var DEFAULT_TEXT = {
    rejected: 'Invalid submission, please check your answers.',
    failed: 'Submission failed, please try again.',
    network_error: 'Network error, please try again.',
    update_failed: 'The form could not update, please try again.',
    choose_one: 'Please choose at least one option.',
    date_unavailable: 'That date is unavailable, please choose another.'
  };
  function text(name) {
    var v = form.getAttribute('data-text-' + name);
    return v === null ? DEFAULT_TEXT[name] : v;
  }
  function reasonText(name, reason) {
    var tmpl = form.getAttribute('data-text-' + name);
    if (tmpl !== null) { return tmpl.split('{reason}').join(reason || ''); }
    return reason || DEFAULT_TEXT[name];
  }
  function showSubmitted() {
    var h = document.createElement('h1');
    var hv = form.getAttribute('data-submitted-heading');
    h.textContent = (hv === null) ? 'Done' : hv;
    var p = document.createElement('p');
    var pv = form.getAttribute('data-submitted-text');
    p.textContent = (pv === null) ? 'Your response has been submitted.' : pv;
    document.body.innerHTML = '';
    document.body.appendChild(h);
    document.body.appendChild(p);
  }
  function isEmpty(v) {
    return v === undefined || v === null || v === '' || (Array.isArray(v) && v.length === 0);
  }
  function collect(forAnswer) {
    var values = {};
    var controls = form.querySelectorAll('[data-field]');
    for (var i = 0; i < controls.length; i++) {
      var el = controls[i];
      if (forAnswer) {
        var row = el.closest('[data-field-row]');
        if (row && row.hidden) { continue; }
      }
      var name = el.getAttribute('data-field');
      var kind = el.getAttribute('data-kind');
      if (kind === 'boolean') {
        values[name] = el.checked;
      } else if (kind === 'array') {
        if (!(name in values)) { values[name] = []; }
        if (el.checked) { values[name].push(el.value); }
      } else if (kind === 'arraytext') {
        var lines = el.value.split(/\\r?\\n/).map(function (s) { return s.trim(); }).filter(Boolean);
        if (lines.length) { values[name] = lines; }
      } else if (el.type === 'radio') {
        if (el.checked) { values[name] = el.value; }
      } else if (kind === 'number') {
        if (el.value !== '') { values[name] = Number(el.value); }
      } else {
        if (el.value !== '') { values[name] = el.value; }
      }
    }
    Object.keys(values).forEach(function (k) {
      if (Array.isArray(values[k]) && values[k].length === 0) { delete values[k]; }
    });
    return values;
  }
  function evalVisible(vw, values) {
    var v = values[vw.field];
    if ('equals' in vw) { return v === vw.equals; }
    if ('in' in vw) { return vw['in'].indexOf(v) !== -1; }
    return !isEmpty(v);
  }
  function applyConditional() {
    var values = collect(false);
    for (var i = 0; i < rows.length; i++) {
      var vw;
      try { vw = JSON.parse(rows[i].getAttribute('data-visible-when')); } catch (e) { continue; }
      rows[i].hidden = !evalVisible(vw, values);
    }
  }
  function dateUnavailable(el) {
    var raw = el.getAttribute('data-unavailable');
    if (!raw || !el.value) { return false; }
    var list; try { list = JSON.parse(raw); } catch (e) { return false; }
    var p = el.value.split('-');
    var d = new Date(Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2])));
    var weekday = WEEKDAYS[d.getUTCDay()];
    for (var i = 0; i < list.length; i++) {
      var entry = String(list[i]).toLowerCase();
      if (entry === el.value || entry === weekday) { return true; }
    }
    return false;
  }
  function fillReadback(step) {
    var dl = step.querySelector('[data-readback]');
    if (!dl) { return; }
    var values = collect(true);
    dl.innerHTML = '';
    Object.keys(labels).forEach(function (name) {
      if (!(name in values)) { return; }
      var dt = document.createElement('dt'); dt.textContent = labels[name];
      var dd = document.createElement('dd');
      dd.textContent = Array.isArray(values[name]) ? values[name].join(', ') : String(values[name]);
      dl.appendChild(dt); dl.appendChild(dd);
    });
  }
  function controlsFor(name) {
    var all = form.querySelectorAll('[data-field]');
    var out = [];
    for (var i = 0; i < all.length; i++) {
      if (all[i].getAttribute('data-field') === name) { out.push(all[i]); }
    }
    return out;
  }
  function rowFor(name) {
    var allRows = form.querySelectorAll('[data-field-row]');
    for (var i = 0; i < allRows.length; i++) {
      if (allRows[i].getAttribute('data-field-row') === name) { return allRows[i]; }
    }
    return null;
  }
  function setFieldValue(name, value) {
    var cs = controlsFor(name);
    for (var i = 0; i < cs.length; i++) {
      var el = cs[i];
      var kind = el.getAttribute('data-kind');
      if (kind === 'boolean') {
        el.checked = (value === true);
      } else if (kind === 'array') {
        el.checked = Array.isArray(value) && value.indexOf(el.value) !== -1;
      } else if (kind === 'arraytext') {
        el.value = Array.isArray(value) ? value.join('\\n') : '';
      } else if (el.type === 'radio') {
        el.checked = (String(el.value) === String(value));
      } else {
        el.value = (value === undefined || value === null) ? '' : value;
      }
    }
  }
  function applyValues(values) {
    Object.keys(values).forEach(function (name) { setFieldValue(name, values[name]); });
  }
  function optionLabel(o) {
    return (o.label !== undefined && o.label !== null && o.label !== '') ? String(o.label) : String(o.value);
  }
  function optionSecondLine(o) {
    return (typeof o.description === 'string' && o.description.trim() !== '') ? o.description : null;
  }
  function rebuildChoices(name, options) {
    if (!Array.isArray(options)) { return; }
    var cs = controlsFor(name);
    if (!cs.length) { return; }
    var first = cs[0];
    if (first.tagName === 'SELECT') {
      var prev = first.value;
      var blankText = (first.options.length && first.options[0].value === '')
        ? first.options[0].textContent : '\\u2014';
      first.innerHTML = '';
      var blank = document.createElement('option');
      blank.value = ''; blank.textContent = blankText;
      first.appendChild(blank);
      options.forEach(function (o) {
        var opt = document.createElement('option');
        var second = optionSecondLine(o);
        opt.value = o.value;
        opt.textContent = second === null ? optionLabel(o) : optionLabel(o) + ' \\u2014 ' + second;
        first.appendChild(opt);
      });
      first.value = prev;
      return;
    }
    var fieldset = first.closest('fieldset');
    if (!fieldset) { return; }
    var isArray = (first.getAttribute('data-kind') === 'array');
    var isChip = fieldset.classList.contains('chips');
    var wasRequired = first.hasAttribute('required');
    var selected = {};
    for (var i = 0; i < cs.length; i++) { if (cs[i].checked) { selected[cs[i].value] = true; } }
    var old = fieldset.querySelectorAll('label.choice');
    for (var j = 0; j < old.length; j++) { old[j].parentNode.removeChild(old[j]); }
    options.forEach(function (o) {
      var label = document.createElement('label');
      label.className = isChip ? 'choice chip' : 'choice';
      var input = document.createElement('input');
      input.setAttribute('type', isArray ? 'checkbox' : 'radio');
      if (!isArray) { input.setAttribute('name', name); }
      input.setAttribute('data-field', name);
      input.setAttribute('data-kind', isArray ? 'array' : 'string');
      input.setAttribute('value', o.value);
      if (!isArray && wasRequired) { input.setAttribute('required', ''); }
      if (selected[o.value]) { input.checked = true; }
      label.appendChild(input);
      label.appendChild(document.createTextNode(' ' + optionLabel(o)));
      var second = optionSecondLine(o);
      if (second !== null) {
        var small = document.createElement('small');
        small.className = 'choice-desc';
        small.textContent = second;
        label.appendChild(small);
      }
      fieldset.appendChild(label);
    });
  }
  function clearFieldErrors() {
    var prev = form.querySelectorAll('.field-error');
    for (var i = 0; i < prev.length; i++) { prev[i].parentNode.removeChild(prev[i]); }
  }
  // A field's row, or (for an END drawn inside a grouped date range) the row its control sits in.
  function rowOfField(name) {
    var row = rowFor(name);
    if (row) { return row; }
    var cs = controlsFor(name);
    return cs.length ? cs[0].closest('[data-field-row]') : null;
  }
  function pinFieldError(name, message) {
    var row = rowOfField(name);
    if (!row) { return null; }
    var box = document.createElement('div');
    box.className = 'field-error';
    box.setAttribute('role', 'alert');
    box.textContent = message;
    row.appendChild(box);
    return row;
  }
  function applyErrors(errors) {
    clearFieldErrors();
    Object.keys(errors).forEach(function (name) { pinFieldError(name, errors[name]); });
  }
  // The sentence for a control's broken shape: the caller's own (data-invalid), else the
  // platform's sentence for that kind (data-msg-<kind>), else the page's refusal line.
  function shapeSentence(el, kind) {
    var own = el.getAttribute('data-invalid');
    if (own !== null) { return own; }
    var platform = el.getAttribute('data-msg-' + kind);
    return platform !== null ? platform : text('rejected');
  }
  // A regex the browser cannot compile (another dialect's syntax) is left to the answer door,
  // which checks every shape again and refuses with the field named.
  function regexFails(source, value) {
    var re;
    try { re = new RegExp(source); } catch (e) { return false; }
    return !re.test(value);
  }
  function dayNumber(iso) {
    var p = iso.split('-');
    return Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2])) / 86400000;
  }
  // The control's first broken shape as its sentence, or null. Native constraint states map to
  // their kind; the schema pattern and the platform's format regex are tested with an unanchored
  // RegExp search (JSON Schema's semantics); a range END checks its order and inclusive span
  // against its START. An empty value is unfilled — a required one is the browser's own check.
  function shapeError(el) {
    var v = el.validity;
    if (v && (v.badInput || v.stepMismatch)) { return shapeSentence(el, 'type'); }
    if (v && v.typeMismatch) { return shapeSentence(el, 'format'); }
    if (v && v.rangeUnderflow) { return shapeSentence(el, el.type === 'date' ? 'min_date' : 'minimum'); }
    if (v && v.rangeOverflow) { return shapeSentence(el, el.type === 'date' ? 'max_date' : 'maximum'); }
    var value = (typeof el.value === 'string') ? el.value : '';
    if (value === '' || el.type === 'checkbox' || el.type === 'radio' || el.tagName === 'SELECT') { return null; }
    var length = Array.from(value).length;
    if (el.minLength > 0 && length < el.minLength) { return shapeSentence(el, 'min_length'); }
    if (el.maxLength >= 0 && length > el.maxLength) { return shapeSentence(el, 'max_length'); }
    var formatPattern = el.getAttribute('data-format-pattern');
    if (formatPattern !== null && regexFails(formatPattern, value)) { return shapeSentence(el, 'format'); }
    var pattern = el.getAttribute('data-pattern');
    if (pattern !== null && regexFails(pattern, value)) { return shapeSentence(el, 'pattern'); }
    var start = el.getAttribute('data-range-start');
    if (start !== null) {
      var startControl = controlsFor(start)[0];
      if (startControl && startControl.value) {
        var days = dayNumber(value) - dayNumber(startControl.value) + 1;
        if (days < 1) { return shapeSentence(el, 'range_order'); }
        var low = el.getAttribute('data-min-days');
        var high = el.getAttribute('data-max-days');
        if ((low !== null && days < Number(low)) || (high !== null && days > Number(high))) {
          return shapeSentence(el, 'range_span');
        }
      }
    }
    return null;
  }
  function applyDisplay(display) {
    var slots = form.querySelectorAll('[data-slot]');
    for (var i = 0; i < slots.length; i++) {
      var slot = slots[i].getAttribute('data-slot');
      if (Object.prototype.hasOwnProperty.call(display, slot)) {
        if (slots[i].tagName === 'IMG') { slots[i].setAttribute('src', String(display[slot])); }
        else { slots[i].textContent = String(display[slot]); }
      }
    }
  }
  function applyUpdate(update) {
    if (!update || typeof update !== 'object') { return; }
    if (update.values) { applyValues(update.values); }
    if (update.options) { Object.keys(update.options).forEach(function (n) { rebuildChoices(n, update.options[n]); }); }
    if (update.display) { applyDisplay(update.display); }
    applyErrors(update.errors || {});
    applyConditional();
  }
  function postReaction(event, cb) {
    if (!reactions) { if (cb) { cb(null); } return; }
    fetch(reactUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event: event, values: collect(true) })
    }).then(function (resp) {
      if (resp.status === 200) {
        return resp.json().then(function (data) {
          var update = (data && data.data && data.data.update) ? data.data.update : {};
          applyUpdate(update);
          if (cb) { cb(update); }
        });
      }
      return resp.json().then(function (data) {
        err.textContent = reasonText('update_failed', data && data.error);
        if (cb) { cb(null); }
      }).catch(function () {
        err.textContent = reasonText('update_failed');
        if (cb) { cb(null); }
      });
    }).catch(function () {
      err.textContent = text('network_error');
      if (cb) { cb(null); }
    });
  }
  function maybeReactField(target) {
    if (!reactions || !target || !target.getAttribute) { return; }
    var name = target.getAttribute('data-field');
    if (name && reactions.field_changed && reactions.field_changed.indexOf(name) !== -1) {
      postReaction({ kind: 'field_changed', field: name });
    }
  }
  function sendAnswer() {
    var answer = collect(true);
    fetch(window.location.href, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: answer })
    }).then(function (resp) {
      if (resp.status === 200) {
        showSubmitted();
        return;
      }
      if (resp.status === 400) {
        resp.json().then(function (data) {
          var reason = data && data.error;
          var row = null;
          // The door names the refused field: pin its sentence under that field once, then show
          // the step that holds it (showing a step keeps every pinned box). With a box pinned, the
          // refusal line reads the caller's data-text-rejected sentence with its {reason} filled by
          // the door's sentence when the caller set one, else the page's own line; with none pinned,
          // the caller's sentence filled the same way, else the door's sentence itself.
          if (data && typeof data.field === 'string' && typeof reason === 'string') {
            clearFieldErrors();
            row = pinFieldError(data.field, reason);
            var step = row ? row.closest('.step') : null;
            for (var s = 0; step && s < steps.length; s++) {
              if (steps[s] === step && s !== current) {
                current = s;
                show(current);
              }
            }
          }
          err.textContent = row === null ? reasonText('rejected', reason)
            : (form.getAttribute('data-text-rejected') === null ? text('rejected') : reasonText('rejected', reason));
        }).catch(function () {
          err.textContent = reasonText('rejected');
        });
        return;
      }
      err.textContent = text('failed');
    }).catch(function () {
      err.textContent = text('network_error');
    });
  }
  function show(i) {
    for (var s = 0; s < steps.length; s++) { steps[s].hidden = (s !== i); }
    back.hidden = (i === 0);
    var last = (i === steps.length - 1);
    next.hidden = last;
    submit.hidden = !last;
    // The shown step's primary button reads its own data-button-label (the caller's word for
    // this page's Next/Submit), else the surface's generic word.
    var bl = steps[i].getAttribute('data-button-label');
    next.textContent = (!last && bl) ? bl : 'Next';
    submit.textContent = (last && bl) ? bl : 'Submit';
    err.textContent = '';
    applyConditional();
    fillReadback(steps[i]);
    var focusTarget = steps[i].querySelector('[data-field]')
      || steps[i].querySelector('input, select, textarea')
      || steps[i].querySelector('h2');
    if (focusTarget) { focusTarget.focus(); }
  }
  function stepValid(i) {
    clearFieldErrors();
    var controls = steps[i].querySelectorAll('[data-field]');
    for (var c = 0; c < controls.length; c++) {
      var el = controls[c];
      var crow = el.closest('[data-field-row]');
      if (crow && crow.hidden) { continue; }
      if (el.validity && el.validity.valueMissing) { el.reportValidity(); return false; }
      var problem = shapeError(el);
      if (problem !== null) {
        pinFieldError(el.getAttribute('data-field'), problem);
        el.focus();
        return false;
      }
      if (el.checkValidity && !el.checkValidity()) { el.reportValidity(); return false; }
    }
    var groups = steps[i].querySelectorAll('[data-array-required="1"]');
    for (var g = 0; g < groups.length; g++) {
      if (groups[g].hidden) { continue; }
      if (!groups[g].querySelector('input[type="checkbox"]:checked')) {
        err.textContent = text('choose_one');
        return false;
      }
    }
    var dates = steps[i].querySelectorAll('input[data-unavailable]');
    for (var d = 0; d < dates.length; d++) {
      var drow = dates[d].closest('[data-field-row]');
      if (drow && drow.hidden) { continue; }
      if (dateUnavailable(dates[d])) {
        err.textContent = text('date_unavailable');
        dates[d].focus();
        return false;
      }
    }
    return true;
  }
  back.addEventListener('click', function () { if (current > 0) { current--; show(current); } });
  next.addEventListener('click', function () {
    if (!stepValid(current) || current >= steps.length - 1) { return; }
    var title = steps[current].getAttribute('data-page-title');
    if (reactions && reactions.page_advanced && title && reactions.page_advanced.indexOf(title) !== -1) {
      postReaction({ kind: 'page_advanced', page: title }, function () { current++; show(current); });
    } else {
      current++; show(current);
    }
  });
  form.addEventListener('input', applyConditional);
  form.addEventListener('change', function (ev) { applyConditional(); maybeReactField(ev.target); });
  show(0);
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    err.textContent = '';
    if (!stepValid(current)) { return; }
    if (reactions && reactions.submitted) {
      // The consumer checks the whole form before acceptance: a submitted reaction that
      // returns per-field errors (or that fails) keeps the form open; only a clean one
      // sends the answer. The answer door stays the authoritative schema check.
      postReaction({ kind: 'submitted' }, function (update) {
        if (update === null) { return; }
        if (update.errors && Object.keys(update.errors).length) { return; }
        sendAnswer();
      });
    } else {
      sendAnswer();
    }
  });
})();
</script>
"""
