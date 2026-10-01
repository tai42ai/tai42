"""Byte-constant HTML pages and response-header sets for the unauthenticated interactions callback surface."""

from __future__ import annotations

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
# ``connect-src``/``form-action`` pinned to ``'self'`` — no external origins, no
# ``unsafe-eval`` — over the same locked-down ``default-src 'none'`` base.
_FORM_HTML_HEADERS = {
    **_BASE_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# Byte-constant pages: zero interpolation of any request-derived value. The
# confirm form posts to the SAME URL (empty action preserves the query string).
_CONFIRM_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>Confirm</title>\n'
    "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}"
    "button{font-size:1rem;padding:.6rem 1.4rem}</style></head><body>\n"
    "<h1>Confirm</h1>\n"
    "<p>Click confirm to submit your response.</p>\n"
    '<form method="post"><button type="submit">Confirm</button></form>\n'
    "</body></html>\n"
)
_DONE_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>Done</title>\n'
    "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}</style></head><body>\n"
    "<h1>Done</h1>\n"
    "<p>This interaction has already been answered.</p>\n"
    "</body></html>\n"
)
# Served on GET for a ticketed text/select question (channel delivery mints a
# ticket for every format): a value answer is required, so the page presents no
# form — the confirm button's empty-body POST could never succeed here.
_REPLY_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>Awaiting reply</title>\n'
    "<style>body{font-family:system-ui,sans-serif;margin:3rem;text-align:center}</style></head><body>\n"
    "<h1>Awaiting your reply</h1>\n"
    "<p>Answer this question by replying on the channel where you received it.</p>\n"
    "</body></html>\n"
)

# The constant submit script for the form page. It drives the stepped pages (showing one
# ``.step`` section at a time, wiring Back/Next, revealing Submit only on the last step — a
# single-step form hides Back/Next), collects EVERY rendered field across all steps (coercing
# numbers, booleans, radio choices and multi-select / list fields, omitting empty optional
# fields), shows/hides each conditional field by its ``visibleWhen`` predicate (a hidden field
# is dropped from the answer), fills a review step's readback from the entered values, and
# best-effort-rejects an unavailable date before POSTing the union as ``{"answer": {...}}`` JSON
# to THIS same callback URL. On 200 it swaps to a done state; on 400 it renders the door's own
# error text and lets the visitor retry.
#
# When the form declares reactions (``data-reactions`` on the form), it also runs the on-change
# reaction round-trip: a change to a declared field, an advance from a declared page, or submit
# (when the form declares a submitted check) POSTs ``{"event": {kind, field?|page?}, "values":
# {...}}`` to the ``/react`` sibling of this callback URL (the ticket react door — the ticket
# stays in the address) and applies the returned form update — setting values, replacing an
# option-bearing field's choice list, showing per-field errors, and filling display slots. A
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
    return (o.label !== undefined && o.label !== null) ? o.label : o.value;
  }
  function rebuildChoices(name, options) {
    if (!Array.isArray(options)) { return; }
    var cs = controlsFor(name);
    if (!cs.length) { return; }
    var first = cs[0];
    if (first.tagName === 'SELECT') {
      var prev = first.value;
      first.innerHTML = '';
      var blank = document.createElement('option');
      blank.value = ''; blank.textContent = '\\u2014';
      first.appendChild(blank);
      options.forEach(function (o) {
        var opt = document.createElement('option');
        opt.value = o.value; opt.textContent = optionLabel(o);
        first.appendChild(opt);
      });
      first.value = prev;
      return;
    }
    var fieldset = first.closest('fieldset');
    if (!fieldset) { return; }
    var isArray = (first.getAttribute('data-kind') === 'array');
    var wasRequired = first.hasAttribute('required');
    var selected = {};
    for (var i = 0; i < cs.length; i++) { if (cs[i].checked) { selected[cs[i].value] = true; } }
    var old = fieldset.querySelectorAll('label.choice');
    for (var j = 0; j < old.length; j++) { old[j].parentNode.removeChild(old[j]); }
    options.forEach(function (o) {
      var label = document.createElement('label');
      label.className = 'choice';
      var input = document.createElement('input');
      input.type = isArray ? 'checkbox' : 'radio';
      input.setAttribute('data-field', name);
      input.setAttribute('data-kind', isArray ? 'array' : 'string');
      if (!isArray) { input.name = name; if (wasRequired) { input.required = true; } }
      input.value = o.value;
      if (selected[o.value]) { input.checked = true; }
      label.appendChild(input);
      label.appendChild(document.createTextNode(' ' + optionLabel(o)));
      fieldset.appendChild(label);
    });
  }
  function applyErrors(errors) {
    var prev = form.querySelectorAll('.field-error');
    for (var i = 0; i < prev.length; i++) { prev[i].parentNode.removeChild(prev[i]); }
    Object.keys(errors).forEach(function (name) {
      var row = rowFor(name);
      if (!row) { return; }
      var box = document.createElement('div');
      box.className = 'field-error';
      box.setAttribute('role', 'alert');
      box.textContent = errors[name];
      row.appendChild(box);
    });
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
        err.textContent = (data && data.error) ? data.error : 'The form could not update, please try again.';
        if (cb) { cb(null); }
      }).catch(function () {
        err.textContent = 'The form could not update, please try again.';
        if (cb) { cb(null); }
      });
    }).catch(function () {
      err.textContent = 'Network error, please try again.';
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
        document.body.innerHTML = '<h1>Done</h1><p>Your response has been submitted.</p>';
        return;
      }
      if (resp.status === 400) {
        resp.json().then(function (data) {
          err.textContent = (data && data.error) ? data.error : 'Invalid submission, please check your answers.';
        }).catch(function () {
          err.textContent = 'Invalid submission, please check your answers.';
        });
        return;
      }
      err.textContent = 'Submission failed, please try again.';
    }).catch(function () {
      err.textContent = 'Network error, please try again.';
    });
  }
  function show(i) {
    for (var s = 0; s < steps.length; s++) { steps[s].hidden = (s !== i); }
    back.hidden = (i === 0);
    var last = (i === steps.length - 1);
    next.hidden = last;
    submit.hidden = !last;
    err.textContent = '';
    applyConditional();
    fillReadback(steps[i]);
    var focusTarget = steps[i].querySelector('[data-field]')
      || steps[i].querySelector('input, select, textarea')
      || steps[i].querySelector('h2');
    if (focusTarget) { focusTarget.focus(); }
  }
  function stepValid(i) {
    var controls = steps[i].querySelectorAll('[data-field]');
    for (var c = 0; c < controls.length; c++) {
      var crow = controls[c].closest('[data-field-row]');
      if (crow && crow.hidden) { continue; }
      if (controls[c].checkValidity && !controls[c].checkValidity()) { controls[c].reportValidity(); return false; }
    }
    var groups = steps[i].querySelectorAll('[data-array-required="1"]');
    for (var g = 0; g < groups.length; g++) {
      if (groups[g].hidden) { continue; }
      if (!groups[g].querySelector('input[type="checkbox"]:checked')) {
        err.textContent = 'Please choose at least one option.';
        return false;
      }
    }
    var dates = steps[i].querySelectorAll('input[data-unavailable]');
    for (var d = 0; d < dates.length; d++) {
      var drow = dates[d].closest('[data-field-row]');
      if (drow && drow.hidden) { continue; }
      if (dateUnavailable(dates[d])) {
        err.textContent = 'That date is unavailable, please choose another.';
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
