# tai42-channel-web

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A **public web chat** channel plugin for the TAI ecosystem. It hosts a standalone
chat page for anonymous visitors, delivers an `ask_user` question into the page the
visitor is looking at, and bridges their reply back into the interactions store — so
an agent can talk to whoever opens the page, not only to people who already have an
account. It implements the `tai42_contract.channels.Channel` protocol and registers
under the name `"web"`. Unlike the vendor channels (WhatsApp, Telegram, Slack) it
carries **no vendor secret and needs no provider account**: the plugin is both the
chat surface and the transport, and a visitor's only credential is the session
cookie the page mints for them.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Channel` is
"how a question reaches a human" — a pluggable deliverer the runtime resolves by
name when `ask_user` is called with `channel=...`. This package is one such
deliverer (public web chat); siblings back the same contract with WhatsApp,
Telegram, or Slack. This repo is this plugin's own full doc home, and the
documentation site covers the platform-level story:

- Interactions concept: https://tai42.ai/concepts/interactions
- Build a channel plugin (author guide): https://tai42.ai/guides/authors/channel
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Channel` protocol,
`ChannelDelivery`, `ChannelDeliveryError`, and the `tai42_app` handle) and
`tai42-kit[redis]` (`HttpxClient`, `RedisClient`, `TaiBaseSettings`, and the
settings cache). Beyond those it depends on `httpx`, `starlette`, and `pydantic` /
`pydantic-settings`.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-channel-web
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/channel-web
```

## Discovery

The runtime discovers the channel through the manifest's `channel_modules` key:

```yaml
channel_modules: ["tai42_channel_web"]
```

At app load the runtime imports every module under the package, and `register.py`
fires the registrations as its import side-effect: the `"web"` channel on
`tai42_app.channels`, and — via the `routes` import — the public
`/api/channels/web` doors on `tai42_app.http`. A bare `import tai42_channel_web`
registers **nothing** — the package is library-safe; only the register module
carries the side-effect. The plugin contributes no Studio UI: its surface is the
public chat page it serves itself.

## The chat page

Create a web route (`channel: web`) with an identity, then send visitors to:

```
https://<your deployment>/api/channels/web/chat/<identity>
```

The page is served for any identity — a name with no web route behind it surfaces
as a friendly "this chat is not available" on the visitor's first message rather
than as a dead URL. The page itself is a plugin-rendered HTML shell around a built
bundle that ships inside the wheel (`src/tai42_channel_web/public/`); an unbuilt
bundle is a loud `500` naming the build step, never a blank page.

## Invite links

An invite link is the chat page URL with a `?tai_pair=<code>` query parameter carrying
a conversation pair code (`LINK-` followed by 8 `[A-Z0-9]` characters):

```
https://<your deployment>/api/channels/web/chat/<identity>?tai_pair=LINK-ABCD1234
```

On load the page submits that code **once**, as the visitor's first message —
exactly as if they had typed it, so the conversation bridge's intercept redeems it
— and then strips the `tai_pair` parameter from the URL, so a reload or a re-shared
link never resubmits it. Only a value that FULLY matches the code shape is acted
on; any other `tai_pair` value is ignored entirely — never submitted, never stripped,
never reflected back into the page — and the rest of the URL is left intact.
Because the session cookie is already minted by the navigation that served the
page, an invite re-pairs a returning visitor in a single load — the counterpart to
the cookie-bound identity in [Limits](#limits): a visitor who cleared cookies is a
new person until an invite (or a typed code) links them again.

## Link parameters

The chat page URL may carry arbitrary query parameters, and the platform captures
them with the visitor's session and delivers them to the turn. A route whose target
is a **tool** receives them on its payload under a `params` key — strings,
jq-reachable as `.params.<name>`:

```
https://<your deployment>/api/channels/web/chat/<identity>?topic=onboarding&ref=abc
```

- The parameters are captured on the navigation that serves the page and stored on
  the visitor's session registration; every later message's turn carries them, so a
  route's `payload_expr` can read `.params.topic`.
- Bounds — a violation is refused as an HTML page, HTTP `400`, carrying
  `link_params_invalid` in its `<meta name="tai42-refusal-code">` and naming the
  first bound it broke: at most **16** parameters; each key matches
  `^[A-Za-z0-9_-]{1,64}$`; each value is at most **512** characters; the whole set
  serializes to at most **2048** bytes.
- Reserved names `tai_pair` (invite links) and `tai_entry` (the entry gate) are seen by
  the duplicate-key check (repeating one is refused too), then stripped, then the
  remaining bounds run — and they are never stored or delivered.
- A later navigation carrying non-empty params **replaces** the stored set (same
  session, same visitor id); a navigation with no params leaves the stored set
  untouched; a `session/rotate` mints a clean session with no params.
- Parameter values never appear in any log line.
- The `params` key is present on the payload **only** when the entry carried
  parameters, and is never merged into the payload root.

An **agent** target receives no params: the platform threads them onto a **tool**
target's payload only. There is no splicing of params into prompt text — that would
be an injection channel — so an agent turn never sees them.

### Params carry no trust

A link parameter is **transport, nothing more**. The platform carries, nests, caps,
and never logs it, but attaches **no** trust: no signature, no expiry, no
interpretation. A param is text anyone can put in a URL. A flow that must *trust* a
value issues its **own** secret token, delivers it in the link, and checks it in its
**own** store — where expiry, single-use, and revocation live — from the tool the
route dispatches (gated in `payload_expr` before the tool runs, or inside the tool
itself). The platform never verifies a param on the flow's behalf.

## Entry gate

A web route can be **gated**: the chat page is served only to a navigation that
carries a live entry code on `?tai_entry=<code>`. An ungated route is byte-identical
to an ungated deployment — the gate is opt-in per identity.

```
https://<your deployment>/api/channels/web/chat/<identity>?tai_entry=<code>
```

Entry codes are **operator-minted**, multi-use, optionally expiring, revocable, and
**hashed at rest** — only a code's SHA-256 is stored; the raw code exists in flight
only, in the mint response and the visitor's URL. A code is
`secrets.token_urlsafe(16)`.

- The gate is checked **only where a session would be minted**: the page-door mint
  path and `session/rotate`. An existing valid session for the identity is admitted
  without a code — a session is an already-granted capability. Code expiry gates
  **new** entries; revocation and the session TTL are the hard-cut tools.
- A missing, unknown, expired, or revoked code — or a throttled client — is refused
  with **one** HTML page, HTTP `403`, one wording, carrying `entry_refused` in its
  `<meta name="tai42-refusal-code">`. The five cases are indistinguishable: the gate
  is **no oracle** for whether a code exists.
- The navigation guard runs **before** the gate check, so a non-navigation is
  refused as `not_a_navigation` regardless of any code it presents — no response ever
  differs by code validity.
- Guessing is throttled per client bucket (`entry_attempts_per_window` attempts per
  `entry_throttle_window_seconds`).
- Every page-door HTML response — the chat page and the refusal pages alike — carries
  `Referrer-Policy: no-referrer`, so a capability URL never leaks through the
  `Referer` header.

`session/rotate` on a gated identity requires the code too: its body takes an
optional `entry_code`, and a missing or dead one is refused `403` with an
`entry_refused` JSON error. The page bundle sends the URL's `tai_entry` value on
rotate when one is present.

### The gate admits a bearer, not a person

The gate says only **"someone holding a live code"** — never **who**. Forwarding the
link forwards entry: anyone who receives the URL enters. It is not identity; identity
remains the pairing's or the flow's own.

**Revocation cuts new entries only.** Revoking a code (or letting it expire) stops it
from admitting the **next** visitor, but an already-admitted session stays valid
until its own TTL lapses — and activity refreshes that TTL. Revocation is not an
eviction tool; to cut a live visitor off, end the session.

### Managing the gate

Four **authed** management doors administer a route's gate. Unlike the public visitor
doors below, these require the platform API key:

- `GET /api/channels/web/gates/{identity}` — the gate's state:
  `{"data": {"enabled": bool, "codes": [{"code_id": <sha256hex>, "label": str|null, "created_at": <iso>, "expires_at": <iso|null>}]}}`.
  Codes are listed by their hash; a raw code is never re-readable.
- `PUT /api/channels/web/gates/{identity}` — turn the gate on or off; body
  `{"enabled": bool}` → `{"data": {"enabled": bool}}`.
- `POST /api/channels/web/gates/{identity}/codes` — mint a code; body
  `{"label": str|null, "expires_at": <iso|null>}` →
  `{"data": {"code": <raw>, "code_id": <sha256hex>, "expires_at": <iso|null>}}`.
  The raw `code` is returned **once**, here, and never again.
- `DELETE /api/channels/web/gates/{identity}/codes/{code_id}` — revoke a code by its
  hash → `{"data": {"status": "revoked"}}`; an unknown id is a `404` envelope error.

## Configuration

Settings are read from the `CHANNEL_WEB_` environment group (see `WebSettings` /
`WebRedisSettings`). There are no credentials:

| Env var | Required | Meaning |
|---|---|---|
| `CHANNEL_WEB_REDIS_URL` | yes | Session registrations + transcript replay store (plugin-owned Redis); falls back to `TAI_DEFAULT_REDIS_URL` |
| `CHANNEL_WEB_REDIS_MAX_CONNECTIONS` … | no | The rest of the kit `RedisConnectionSettings` fields, same names under this prefix |
| `CHANNEL_WEB_PAGE_TITLE` | no (`Chat`) | The chat page's `<title>` |
| `CHANNEL_WEB_SESSION_TTL_SECONDS` | no (2592000) | Lifetime of the session cookie and its server-side registration, refreshed on every door that resolves it |
| `CHANNEL_WEB_SESSION_PENDING_TTL_SECONDS` | no (600) | Lifetime a *freshly minted* registration gets until its cookie first comes back; then it is promoted to the full TTL |
| `CHANNEL_WEB_SESSION_COOKIE_SECURE` | no (true) | `Secure` on that cookie, and with it the cookie's name and `Path` (see [Sessions](#sessions)); set false ONLY for a plain-http dev/e2e origin |
| `CHANNEL_WEB_HTTP_TIMEOUT_SECONDS` | no (30.0) | Answer-forward timeout to the interactions callback door, seconds |
| `CHANNEL_WEB_MAX_BODY_BYTES` | no (65536) | Bounded-read cap on the message and answer doors; over-cap is a `413` |
| `CHANNEL_WEB_MAX_STREAMS_PER_VISITOR` | no (4) | Concurrent SSE streams one visitor may hold; over is a `503` |
| `CHANNEL_WEB_MAX_STREAMS_TOTAL` | no (500) | Concurrent SSE streams this process may hold, across all visitors |
| `CHANNEL_WEB_MAX_ANSWER_RESTORES` | no (5) | How often one question's record is put back after a refused forward before it is left dropped |
| `CHANNEL_WEB_TRANSCRIPT_MAX_ENTRIES` | no (1000) | Exact XADD `MAXLEN` cap on a conversation's replay stream |
| `CHANNEL_WEB_TRANSCRIPT_TTL_SECONDS` | no (2592000) | Idle-transcript TTL, refreshed on every append |
| `CHANNEL_WEB_BACKLOG_BATCH_ENTRIES` | no (200) | Transcript entries read per page when a stream replays its backlog |
| `CHANNEL_WEB_KEEPALIVE_SECONDS` | no (15) | SSE keepalive cadence and the live-tail block window |
| `CHANNEL_WEB_BLOCKING_GRACE_SECONDS` | no (5.0) | Grace added to the tail's outer `wait_for`; a stalled Redis XREAD then raises loudly |
| `CHANNEL_WEB_ENTRY_ATTEMPTS_PER_WINDOW` | no (10) | [Entry gate](#entry-gate): entry-code guesses one client bucket may make per throttle window |
| `CHANNEL_WEB_ENTRY_THROTTLE_WINDOW_SECONDS` | no (300) | [Entry gate](#entry-gate): the throttle window, seconds |

Flood control on these public doors is **not** this plugin's. Every
`/api/channels/web/*` door is public, so the app-level rate limiter throttles it as
the `channels_web` family (requests per caller, tuned via
`TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB__LIMIT` / `__BURST` / `__ENABLED`, bucketed
with `TAI_RATE_LIMIT_TRUSTED_PROXIES` behind a reverse proxy), and the operator's
ingress bounds what reaches the platform at all. What the plugin bounds is the one
resource it owns — the dedicated store connection an open SSE stream pins, capped
per visitor and per process — and it reads no client address to do it.

## Sessions

A visitor is anonymous, and their session is **two** ids, minted and registered
together when they open the chat page:

- a **secret token** (`secrets.token_urlsafe`, ≥128 bits) in the visitor session
  cookie — `HttpOnly`, `SameSite=Lax`. This is the bearer capability and nothing
  else;
- a **visitor id**, a separate opaque non-secret id the token is registered against
  server-side (`channel:web:session:{token}` in the plugin's Redis). **That** is the
  conversation address (`client_address`), the transcript key, and what an
  `ask_user` names as its recipient.

`SESSION_COOKIE_SECURE` decides the cookie's name, `Path` and `Secure` flag
together, because a browser honours the `__Host-` prefix only on a cookie that is
`Secure`, at `Path=/`, and carries no `Domain` — and in exchange no sibling host of
this origin can plant or overwrite it. So a Secure deployment (the default) mints
`__Host-tai_web_session` at `/`, and a plain-http one (dev / e2e), which can satisfy
none of the three conditions, mints the bare `tai_web_session` scoped to
`/api/channels/web`, where the page, its assets and the chat doors all live. The two
names are never accepted interchangeably: the mode decides one name, and a cookie
under the other is not this deployment's.

Every door reads the cookie, resolves it through the registration, and uses only the
visitor id — never the cookie value, and never a body or query value. A token with
no registration behind it is not a session: an invented cookie opens no conversation
(so it cannot mint a fresh address past the bridge's per-address turn caps), and a
planted one is replaced rather than adopted when the page loads. Resolving a session
refreshes both the cookie's `Max-Age` and the registration's TTL. A visitor with no
session gets `401` + `"code": "session_missing"`, which the page answers by
re-opening the chat URL.

A session is a capability on **one web route**. The registration records the
identity it was minted on, and a door presented with it on any other identity
refuses exactly as it refuses an unknown token — the same `401` +
`"code": "session_missing"`, so no session can be probed for which route it belongs
to. One browser holding sessions for two routes therefore holds two separate
conversations, and the chat page of a route the cookie does not serve mints a fresh
session rather than adopting the foreign one.

A *freshly minted* registration only lives `SESSION_PENDING_TTL_SECONDS`; the first
time its cookie comes back it is promoted to the full `SESSION_TTL_SECONDS`. A
cookie-less GET loop therefore leaves minute-lived keys behind, not one 30-day
registration per request. Minting is guarded once more: the page mints only for a
top-level navigation (`Sec-Fetch-Dest: document`, tolerating browsers that omit it),
so a cross-site subresource cannot overwrite a live visitor's cookie.

`POST /api/channels/web/session/rotate` deletes the old registration and mints a
fresh token + visitor id for the web route its body names — the page's "new
conversation". The old token can never address the old conversation again; that
transcript is untouched and ages out on its own TTL.

## Doors

Every door is public (`authed=False`); the visitor's session cookie is the only
credential, and no door takes a conversation address from a body or query value.
Every door that names an identity serves it only when the caller's session was
minted on that same web route, and answers a foreign one exactly as it answers an
unknown token.

- `GET /api/channels/web/chat/{identity}` — the chat page: the HTML shell plus the
  built bundle's hashed `<link>`/`<script>` tags. Mints and registers a session for
  this route whenever the cookie resolves to none, or to one minted on another route
  — a navigation only, else `403` + `"code": "not_a_navigation"`. On a gated route
  the mint additionally requires a live entry code (see [Entry gate](#entry-gate)),
  and the query string's [link parameters](#link-parameters) are captured with the
  session. Carries a strict CSP (`default-src 'none'`,
  `script-src 'self'`, `connect-src 'self'`, `font-src 'self'`, no framing, no
  inline script; `style-src` admits `'unsafe-inline'` because the bundled
  design-system overlays inject a `<style>` element for their scroll lock).

  This door is reached by **navigating** to it, so it never answers JSON: every
  refusal — `403 not_a_navigation`, `403 entry_refused` (a gated route with no live
  code, or a throttled client), `400 link_params_invalid` (params over a bound),
  `501` with no store configured, `500` with no usable bundle — is a minimal HTML
  page, with the status unchanged, and the machine-readable code carried in a
  `<meta name="tai42-refusal-code">` by every refusal that has one. The `500` has
  none: it is a server fault with no JSON counterpart and nothing the page could do
  differently for. Every response from this door — the chat page and the refusal
  pages alike — carries `Referrer-Policy: no-referrer`, so a capability URL in the
  query string never leaks through the `Referer` header. Those pages link, run and
  style nothing, under their own
  `default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`.
- `GET /api/channels/web/assets/{file}` — one file of that bundle. Only names listed
  in the build manifest's integrity map are served (exact-name lookup, explicit
  content-type map, `immutable` caching); anything else is a `404`.
- `POST /api/channels/web/messages` — body `{identity, text}`, optionally
  `client_message_id` → `{message_id}`. The message is bridged through
  `conversations.accept` (channel `"web"`, `our_identity = identity`,
  `client_address = <visitor id>`) and, on success, appended to the visitor's
  transcript so their own stream replays it. The `message_id` in the reply is the id
  the visitor's own `chat.message` frame carries on their stream, so a page that
  drew the message optimistically can match the two. An identity the caller's session
  was not minted on → `401`; unroutable identity → `404`; blank text or a body that
  is not valid JSON → `400`; an unusable identity, an over-long text or a malformed
  `client_message_id` → `422`; an over-cap body → `413`; a full thread queue →
  `503`.

  `client_message_id` is the page's **retry key**: an opaque
  `^[A-Za-z0-9_-]{8,64}$` string the browser keeps for one composed message. A POST
  that succeeded server-side but whose response never arrived is re-sent with the
  same key; the door derives the bridge's `provider_message_id` from that key *and*
  the conversation it goes into (`identity` + the caller's own visitor id), and the
  bridge — idempotent on `(channel, provider_message_id)` — returns the first
  attempt's `message_id` rather than starting a second turn. Omit it and every POST
  mints a fresh id, so every POST is its own turn. Scoping to the whole conversation
  is what stops one visitor from reaching into another's dedup space *and* one web
  route's turn from answering a message sent to a different one (the bridge dedups
  identity-blind); neither half is the caller's to choose. The key is echoed back on
  the visitor's own transcript frame as `client_message_id`, so a page that lost a
  response can retire the optimistic bubble it drew when the replay arrives; the
  field is absent from every frame whose sender sent no key.
- `GET /api/channels/web/stream?identity=<id>` — the SSE feed of the session's own
  conversation, keyed by `(identity, visitor id)`: the transcript backlog, a
  `chat.backlog_done` marker, then a live tail of `chat.message` / `chat.question` /
  `chat.answered` frames with keepalive comments. A missing or unusable `identity`
  → `400`; an `identity` the caller's session was not minted on → `401`; past a
  concurrent-stream cap → `503`, whose message names which ceiling was hit (this
  session's or the whole server's).
- `POST /api/channels/web/questions/{interaction_id}/answer` — body `{answer}` →
  `{status: "answered"}`. The answer is one FINITE scalar (a `text` / `confirm` /
  `select` answer) or a JSON object (a `form` answer, bounded by its serialized UTF-8
  size — at most 32 KiB — rather than the scalar character cap): `400` for a body that
  is not valid JSON or carries no `answer`, `413` over the body cap, `422` for a value
  that is neither scalar nor object, an over-long string, a non-finite number, or a
  form object over its byte cap or carrying a non-finite number (`Infinity`/`NaN`
  would forward invalid JSON and persist an unparseable transcript frame). The
  callback door stays authoritative on the answer's format and schema match. The
  pending record must belong to the caller's own conversation — both its web route identity
  and its address (a foreign one is reported as not found, never as
  "exists, but not yours"); the answer is then forwarded to the interactions callback
  and a `chat.answered` frame appended on success. A callback door that reports the
  question already answered elsewhere → `409`, with no frame appended (the recorded
  answer is not this visitor's). A callback door that refuses the answer → `400`, and
  the record is put back so the visitor can re-answer — up to `MAX_ANSWER_RESTORES`
  times, after which the question is left dropped and the refusal says so.
- `POST /api/channels/web/session/rotate` — body `{identity}`, optionally
  `entry_code` → `{status: "rotated"}`; unregisters the old session and sets a
  freshly minted cookie bound to the web route the body names, with no link
  parameters. No session is required to rotate — anyone may open any route's chat
  page and be minted one there. On a [gated](#entry-gate) identity a rotate carrying
  a missing or dead `entry_code` is refused `403` + `"code": "entry_refused"`.

Every door but the assets door needs the store, and answers `501` +
`"code": "web_transcript_store_off"` when none is configured — without one there is
nowhere to register a session, so nothing downstream can work.

Success bodies are `{"data": {...}}`; failures are `{"error": "<message>"}`, plus a
`"code"` on the ones a caller must tell apart from the status alone:
`session_missing` (401), `origin_mismatch` (403), `not_a_navigation` (403),
`entry_refused` (403, the rotate door on a gated identity) and
`web_transcript_store_off` (501). The chat page door is the exception — its caller is
a browser navigation, so it answers those refusals as HTML pages (above).

## How a delivery is addressed

`ChannelDelivery` carries no sender identity, so a web `ask_user` names its target
as `recipient = "<identity>:<visitor-id>"` — the channel splits it back into the
transcript pair (on the LAST colon: a visitor id is urlsafe-minted and therefore
colon-free) and stores both in the pending-question record so the later
`chat.answered` frame lands on the same stream. A `notify` takes either shape: the
conversation bridge sets `sender_identity` (the web route identity) and a bare
visitor-id `recipient`; `notify_user` never sets `sender_identity` (that field is
the bridge's), so its `recipient` carries the same `"<identity>:<visitor-id>"`
composite a delivery uses. A composite that is not of that shape raises
`ChannelDeliveryError`. All five answer formats (`text` / `confirm` / `select` /
`form` / `external`) are delivered as transcript entries the page renders as widgets;
the `form` entry carries the interaction's answer schema, which the page renders as a
schema-driven form widget (the visitor's answer posts back as a JSON object through
this plugin's own answer door), and only the `external` entry carries the
interaction's `callback_url`, because only its widget opens one. The channel sends
plain text only (no media, no templates).

## Security

- The cookie token's unguessability IS the capability: holding the cookie is holding
  the conversation. It is minted from a CSPRNG at ≥128 bits, kept `HttpOnly` (page
  script never reads it), and accepted back only in the minted alphabet and length —
  and then only when a server-side registration stands behind it.
- The token and the address are different ids. The address is what the bridge, the
  transcript keys, and the operator plane publish, so reading one grants nothing;
  the token is never published anywhere.
- The conversation `client_address` is always the registered visitor id, never a
  client-supplied value — one visitor's stream can only ever be their own
  conversation, and a question is answerable only from the conversation it was asked
  in.
- A session is bound to the web route it was minted on, and every door refuses a
  session presented on another route exactly as it refuses an unknown token. A
  stolen cookie is therefore good for one route only, and the refusal never says
  which one it is good for.
- On a Secure deployment the cookie carries the `__Host-` prefix, so no sibling host
  sharing this registrable domain can plant or overwrite a visitor's session.
- CSRF posture: two independent legs, each of which refuses a cross-site POST on its
  own — `SameSite=Lax` withholds the session cookie from it (the door then resolves
  no session and answers `401`), and an `Origin` check on every POST door refuses the
  mismatched origin a browser attaches to every cross-site POST (`403`). The JSON
  body is not a third leg: a cross-site form can post a JSON-shaped body with
  `enctype="text/plain"`, and no door checks `Content-Type`.
  Behind a proxy the `Origin` check compares
  against `X-Forwarded-Proto` / `X-Forwarded-Host` when they are set. Those headers
  are read with no trusted-proxy check; a browser can only attach them on a
  preflighted cross-origin fetch,
  which these doors — returning no CORS headers — refuse at the preflight. The mint
  path additionally requires a top-level navigation, so a cross-site subresource
  cannot overwrite a live session cookie.
- The message and answer doors read their bodies bounded (`MAX_BODY_BYTES`, actual
  bytes, never a declared `Content-Length`) and refuse an over-cap one with `413`,
  never a truncation. An `ask_user` callback ticket never reaches the browser except
  for the `external` widget that must open it.
- The page's CSP admits scripts, stylesheets, fonts, and connections from its own
  origin only, forbids framing, and the assets door serves only integrity-listed
  files with an explicit content-type (never `text/html`). Inline STYLE is admitted
  (the bundled overlays inject one `<style>` element for their scroll lock); inline
  script is not, so an injected string still has no way to run.
- The transcript is a plugin-owned Redis stream with a bounded `MAXLEN` and a
  refreshed TTL; the durable record of a turn lives in the conversation bridge.
- Every transcript frame is `json.dumps`'d, so a newline or `data:` sequence in a
  message body cannot inject an extra SSE frame.
- Flood control is **not** this plugin's. Abuse control on these public doors belongs
  to the platform's public-door rate limiter, which throttles the whole
  `/api/channels/web/*` family per caller ahead of them, and to the operator's ingress.
  What the plugin bounds is the one resource it owns: an open SSE stream pins a
  dedicated store connection for its whole life, so the stream door caps concurrent
  streams per visitor and per process (a loud `503` over either). Behind those, the
  conversation bridge's per-address turn cap bounds LLM spend — and because a visitor
  mints and rotates their own visitor id, the messages door keys that cap on the
  request's network client bucket (the same value the public-door limiter derives), not
  on the resettable visitor id.
- An answer forward the callback door refuses restores the question so the visitor
  can re-answer, but only `MAX_ANSWER_RESTORES` times: each forward spends a slot of
  that door's own rate limit, which is keyed on this server's egress IP and shared
  with every other channel's answer forwards. What the visitor is told about a
  refusal is the callback door's own error message and nothing else — a reply that is
  not this platform's error envelope (a proxy or WAF page, a traceback) is logged for
  the operator and replaced.

## Limits

| Limit | Consequence |
|---|---|
| No default recipient | A web ask must name its target; a recipientless `deliver`/`notify` raises `ChannelDeliveryError` |
| Plain text only | A media or template notification is refused loudly — this channel advertises no media/template capability |
| Bounded replay | The transcript keeps the newest `TRANSCRIPT_MAX_ENTRIES` entries; older ones are trimmed (the bridge holds the durable record) |
| Single forward attempt | A failed answer forward restores the pending question so the visitor can retry; the door never blind-retries the callback |
| Bounded re-answering | One question is restored at most `MAX_ANSWER_RESTORES` times; after that it is left dropped and resolves by its own timeout |
| Cookie-bound conversation | A visitor who clears cookies (or opens another browser) starts a new conversation; there is no account to resume from — an invite link (`?tai_pair=`) re-pairs them in one load |
| Invite links | The chat URL accepts `?tai_pair=<LINK-code>`; the page submits it once as the first message and strips it. A `tai_pair` value that is not a well-formed code is ignored |
| Link parameters | The chat URL's query params are captured and delivered to a **tool** target under `params` (jq-reachable); over a bound (16 / key 64 / value 512 / 2KB) is a refused page. `tai_pair`/`tai_entry` are reserved. The platform attaches no trust — a flow checks its own token in its own store |
| Entry gate | A route can require a live `?tai_entry=` code to serve its page; refusal is uniform (no oracle) and guess-throttled. The gate admits a code-**bearer**, not a person; revocation cuts new entries only |
| One route per session | A session serves the web route it was minted on; a visitor who opens a second route's chat page holds a second, separate conversation |
| No plugin-side flood control | Abuse control on these public doors is the platform limiter's and the operator's ingress; the plugin caps only concurrent SSE streams |
| Store required | Every door but the assets door refuses `501` without `CHANNEL_WEB_REDIS_URL` — a session cannot be registered, so nothing downstream can work |
| Per-process stream cap | `MAX_STREAMS_TOTAL` is counted per worker, so a multi-worker deployment admits that many streams per worker |

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

### The chat page front-end

The page's source lives in `public-src/` (React + `@tai42/studio-sdk`, TypeScript).
`pnpm build` regenerates `src/tai42_channel_web/public/` — the content-hashed
bundle, its stylesheet, its webfonts, and `public-manifest.json` — and that output
is **committed**, because the wheel ships it and the doors serve it. The bundle is
SELF-CONTAINED: React and the design system are compiled in, so the page needs no
import map and loads nothing from anywhere else.

```bash
pnpm install
pnpm build         # regenerates src/tai42_channel_web/public/
pnpm typecheck
pnpm test         # vitest + v8 coverage thresholds
pnpm format:check
```

`tests/test_public_dist.py` re-hashes the committed bundle against its manifest, so
a bundle whose bytes and declared SRI digests have drifted apart fails before it can
ship a page the browser refuses to load. Freshness — a source edit that was never
rebuilt — is CI's job: it rebuilds `src/tai42_channel_web/public/` and diffs the
result against the commit.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
