"""The web-chat doors — under ``/api/channels/web``.

Importing this package registers every door as a side effect: each door submodule
carries its ``@tai42_app.http.custom_route`` declaration, and the imports at the foot
of this module load all six.

The chat doors are PUBLIC; the entry-gate management doors are AUTHED — they carry the
platform api key and declare an explicit action-class.

* ``GET /api/channels/web/chat/{identity}`` — the standalone chat page for one web
  route: the HTML shell around the built bundle. Mints AND registers the visitor's
  session when their cookie resolves to none for THIS route, and refreshes both the
  cookie's ``Max-Age`` and the registration on every load. The navigation's query
  string carries link params (captured with the session, delivered to the turn's tool
  payload) and, on a gated route, the ``tai_entry`` code that admits a new entry.
* ``GET /api/channels/web/assets/{file}`` — one file of that bundle, served only
  when the build manifest's integrity map lists it by exact name.
* ``POST /api/channels/web/messages`` — the visitor sends one message into their own
  conversation. The address comes from the session registration, never the body; the
  message is bridged through ``conversations.accept`` and, on success, appended to
  the transcript so the visitor's SSE stream replays it.
* ``GET /api/channels/web/stream`` — the SSE feed of the session's own conversation
  (backlog then live tail), keyed by ``(identity, visitor id)``.
* ``POST /api/channels/web/questions/{interaction_id}/answer`` — answer a pending
  question: the record must belong to the caller's own conversation, then the answer
  is forwarded to its interactions callback and a ``chat.answered`` frame appended.
  The forwarded answer is one scalar (text/confirm/select) or a JSON object (form);
  this door bounds only its shape and size — the callback door stays authoritative on
  whether it matches the question's stored schema.
* ``POST /api/channels/web/forms/{token}`` — submit an ask-less form card. The token
  names a ``chat.form`` card's stored record, which must belong to the caller's own
  conversation (a foreign or expired token answers ONE uniform 404 — no oracle). The
  values are bounded as pure transport (a non-empty JSON object of finite numbers,
  size-capped) and NEVER validated against the form's schema — participant-shaped data;
  the door renders ``label: value`` text from the STORED schema (server-trusted
  labels) and bridges text + values through ``conversations.accept`` as one participant
  message. The record is read, never claimed: every submission is its own message.
* ``POST /api/channels/web/session/rotate`` — body ``{identity}``; mint a fresh
  session for that web route (the visitor's "new conversation"); the next message
  opens a conversation on the new address.

The chat doors are PUBLIC (declared ``public: true`` in ``tai-plugin.yml``): the
transport credential is the visitor's session cookie, and no chat door reads the
platform api key or a Studio session. A session is a capability on ONE web route: it
is minted against the route it was minted on, and a door presented with it on any other
route refuses exactly as it refuses an unknown token — the two are indistinguishable to
a caller, so no session can be probed for which route it belongs to. Success bodies are
``{"data": {...}}``; failures are ``{"error": "<message>"}``, plus a ``code`` on the
refusals a caller must tell apart from their status alone — ``session_missing`` (401),
``origin_mismatch`` (403), ``not_a_navigation`` (403), ``entry_refused`` (403),
``web_transcript_store_off`` (501).

The chat page door is the exception: its caller is a browser NAVIGATION, which would
render a JSON body as text, so every refusal it answers is a minimal HTML page under
``REFUSAL_CSP`` instead — same status and, for the refusals that have one, the same
machine-readable code carried in a meta tag (``link_params_invalid`` for a params
bound violation, ``entry_refused`` for a gated route with no live code, uniform for
all five of missing/unknown/expired/revoked/throttled — no oracle). The unusable-
bundle 500 has no code: it is a server fault with no API counterpart, and nothing the
page could do differently for. Every page-door HTML response carries
``Referrer-Policy: no-referrer`` — a capability URL must never leak via referrer.

The entry-gate management doors are AUTHED (declared ``public: false``): they mint,
list, revoke codes and toggle the gate for a web route, keyed by the platform api
key, and each declares an explicit ``read``/``write`` action-class (an authed route
with none fails to register). Entry codes are hashed at rest, multi-use, optionally
expiring; the raw code is returned once at mint and never read back. A gate refuses
uniformly.

Flood control on these doors is NOT this plugin's: the platform's public-door limiter
bounds requests per caller ahead of them, and the operator's ingress bounds what
reaches the platform. What the plugin bounds is the one resource it owns — the
dedicated store connection an open SSE stream pins (``stream.py``). The one client
address it reads is on the messages door: the visitor id keys the conversation, but a
visitor mints and rotates that id freely, so the accountable turn cap is keyed on the
request's network client bucket instead — the same value the public-door limiter
derives — never on the resettable visitor id.
"""

from __future__ import annotations

from tai42_channel_web.routes import (  # noqa: F401  (route registration side-effects)
    answer_routes,
    gate_routes,
    message_routes,
    page_routes,
    session_routes,
    stream_routes,
)
