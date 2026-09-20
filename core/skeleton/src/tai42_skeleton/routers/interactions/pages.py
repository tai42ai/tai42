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

# The constant submit script for the form page. It drives the stepped pages
# (showing one ``.step`` section at a time, wiring Back/Next, revealing Submit only
# on the last step — a single-step form hides Back/Next) and, on submit, reads EVERY
# rendered field across all steps, coerces number inputs to numbers and checkboxes to
# booleans, omits empty optional fields, and POSTs the union as ``{"answer": {...}}``
# JSON to THIS same callback URL. On 200 (answered or already_answered) it swaps to a
# done state; on 400 it renders the door's own error text and lets the visitor retry.
# The script body is a constant — only the field markup above it is schema-derived
# (and escaped).
_FORM_SUBMIT_SCRIPT = """<script>
(function () {
  var form = document.getElementById('askform');
  var err = document.getElementById('err');
  var steps = form.querySelectorAll('.step');
  var back = form.querySelector('[data-nav="back"]');
  var next = form.querySelector('[data-nav="next"]');
  var submit = form.querySelector('[data-nav="submit"]');
  var current = 0;
  function show(i) {
    for (var s = 0; s < steps.length; s++) { steps[s].hidden = (s !== i); }
    back.hidden = (i === 0);
    var last = (i === steps.length - 1);
    next.hidden = last;
    submit.hidden = !last;
    err.textContent = '';
    var focusTarget = steps[i].querySelector('[data-field]')
      || steps[i].querySelector('input, select, textarea')
      || steps[i].querySelector('h2');
    if (focusTarget) { focusTarget.focus(); }
  }
  function stepValid(i) {
    var controls = steps[i].querySelectorAll('[data-field]');
    for (var c = 0; c < controls.length; c++) {
      if (controls[c].checkValidity && !controls[c].checkValidity()) { controls[c].reportValidity(); return false; }
    }
    return true;
  }
  back.addEventListener('click', function () { if (current > 0) { current--; show(current); } });
  next.addEventListener('click', function () {
    if (stepValid(current) && current < steps.length - 1) { current++; show(current); }
  });
  show(0);
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    err.textContent = '';
    var answer = {};
    var fields = form.querySelectorAll('[data-field]');
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i];
      var name = el.getAttribute('data-field');
      var kind = el.getAttribute('data-kind');
      if (kind === 'boolean') {
        answer[name] = el.checked;
      } else if (kind === 'number') {
        if (el.value !== '') { answer[name] = Number(el.value); }
      } else {
        if (el.value !== '') { answer[name] = el.value; }
      }
    }
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
  });
})();
</script>
"""
