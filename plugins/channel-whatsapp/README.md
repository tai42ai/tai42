# tai42-channel-whatsapp

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Meta **WhatsApp API** channel plugin for the TAI ecosystem. It delivers
an `ask_user` question to a human on WhatsApp through the Cloud (Graph) API and
bridges the human's reply back into the interactions store — so an agent can
reach a person out-of-band instead of only showing the question in the Studio
inbox. It implements the `tai42_contract.channels.Channel` protocol and registers
under the name `"whatsapp"`. It is for numbers hosted directly on Meta's
Cloud API (no BSP/Twilio in front); the Twilio-hosted path is the sibling
`tai42-channel-twilio`.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Channel`
is "how a question reaches a human" — a pluggable deliverer the runtime resolves
by name when `ask_user` is called with `channel=...`. This package is one such
deliverer (WhatsApp API); siblings back the same contract with Twilio,
Telegram, or Slack. The ecosystem is open-ended: any package can back the same
contract, so this repo is this plugin's own full doc home, and the documentation
site covers the platform-level story:

- Interactions concept: https://tai42.ai/concepts/interactions
- Build a channel plugin (author guide): https://tai42.ai/guides/authors/channel
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Channel` protocol,
`ChannelDelivery`, `ChannelDeliveryError`, and the `tai42_app` handle) and
`tai42-kit[redis]` (`HttpxClient`, `RedisClient`, `TaiBaseSettings`, and the
settings cache). Beyond those it depends on `httpx`, `starlette`, and
`pydantic` / `pydantic-settings`. There is **no Meta SDK**: the send is one
Bearer-auth JSON POST over `httpx`, and webhook signature validation is a few
lines of stdlib `hmac`/`hashlib`.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-channel-whatsapp
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/channel-whatsapp
```

## Discovery

The runtime discovers this plugin through the manifest's `channel_modules` key:

```yaml
channel_modules: ["tai42_channel_whatsapp"]
```

At app load the runtime imports every module under the package, and
`register.py` fires the registrations as its import side-effect: the
`"whatsapp"` channel on `tai42_app.channels`, and — via the `inbound`
import — the unauthenticated webhook route on `tai42_app.http`. A bare
`import tai42_channel_whatsapp` registers **nothing** — the package is
library-safe; only the register module carries the side-effect.

## Configuration

Settings are read from the `CHANNEL_WHATSAPP_` environment group (see
`WhatsAppSettings` / `WhatsAppRedisSettings`):

| Env var | Required | Meaning |
|---|---|---|
| `CHANNEL_WHATSAPP_ACCESS_TOKEN` | yes | Graph API access token (`SecretStr`) — the Bearer credential for the send |
| `CHANNEL_WHATSAPP_APP_SECRET` | yes | Meta app secret (`SecretStr`) — the `X-Hub-Signature-256` HMAC key for inbound webhooks |
| `CHANNEL_WHATSAPP_VERIFY_TOKEN` | yes | Shared token (`SecretStr`) echoed during Meta's GET webhook verification handshake |
| `CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID` | for ask_user | The `phone_number_id` messages are sent FROM when no sender identity is routed |
| `CHANNEL_WHATSAPP_WABA_ID` | for forms | The WhatsApp Business Account id that owns Flows — a `form` ask and an ask-less form notification are rendered as a WhatsApp Flow created and published under this WABA, and the notify-form schema cache is keyed under it. Required only on the form paths |
| `CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS` | for cold templates | Whitelist of `wa_id`s a **template** send may reach when the recipient is not a known contact — comma-separated or a JSON list. Freeform sends are not fenced by it |
| `CHANNEL_WHATSAPP_TEMPLATE_CONTACT_WINDOW_DAYS` | no (30) | Rolling "seen within N days" window admitting a template send to a `wa_id` the inbound webhook has messaged from; `0` disables known-contact tracking (allowlist-only templates) |
| `CHANNEL_WHATSAPP_API_BASE_URL` | no | Graph API origin + pinned version (default `https://graph.facebook.com/v23.0`) |
| `CHANNEL_WHATSAPP_REDIS_URL` | yes | Correlation + known-contact store (plugin-owned Redis) |
| `CHANNEL_WHATSAPP_REDIS_MAX_CONNECTIONS` … | no | The rest of the kit `RedisConnectionSettings` fields, same names under this prefix |
| `CHANNEL_WHATSAPP_HTTP_TIMEOUT_SECONDS` | no (30.0) | Outbound send + answer-forward timeout, seconds |
| `CHANNEL_WHATSAPP_DEDUPE_TTL` | no (172800) | Seen-`wamid` replay-guard window, seconds |

One credential (`ACCESS_TOKEN` + `APP_SECRET`) serves many `phone_number_id`s: a
bridge reply is sent from the exact `phone_number_id` that received the inbound
message, while an ask_user delivery is sent from
`CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID`. Recipient policy is split by what
Meta itself fences. **Freeform** sends (questions, replies, media) reach any
requested `wa_id` — Meta's own 24-hour customer-service window is the fence, so a
send to a number outside it is rejected by Meta (error 131047) and raises.
**Template** sends are the one send Meta delivers cold, so they keep an operator
fence: the recipient must be on `CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS` **or** be a
known contact — a `(phone_number_id, wa_id)` pair the inbound webhook has seen
within `CHANNEL_WHATSAPP_TEMPLATE_CONTACT_WINDOW_DAYS` days; a cold, unlisted
number raises. A recipient is always caller-supplied — there is no default
recipient, so a recipientless request raises. Secrets live only in the
environment.

Two steps happen **out-of-band** (the plugin never mutates Meta app configuration
at startup):

1. In the Meta App dashboard, point the WhatsApp webhook callback URL at
   `{public base URL}/api/channels/whatsapp/inbound` and set the verify
   token to `CHANNEL_WHATSAPP_VERIFY_TOKEN`. Meta issues a `GET` handshake
   the route answers by echoing `hub.challenge`.
2. Subscribe the app to the `messages` webhook field so message and delivery-status
   events reach the same `POST` endpoint.

## How a human answers

A `text` question arrives as a normal WhatsApp message; the human **just
replies** — no code to quote, no prefix. A `select` question renders natively: a
few short options become tappable **reply buttons**, more options become an
interactive **list**, and past those platform caps it falls back to a numbered
plaintext list. A tap answers by a question-bound id that maps back to the exact
option text; the human may always **type** an option instead. A `form` question
renders as an in-chat **WhatsApp Flow** — one screen of typed fields the human
fills and submits: a `string` becomes a text field, a `string` with an `enum`
a dropdown, a `boolean` an opt-in toggle, and an `integer`/`number` a numeric
text field. The Flow is created and published once per distinct answer schema
(cached by a schema hash under `CHANNEL_WHATSAPP_WABA_ID`) and reused; the
completed form returns as an `nfm_reply`, its values coerced to the schema's
types before the answer object is forwarded. When the callback door **rejects**
a forwarded form answer (`400` — a schema rule the Flow could not enforce, e.g. a
minimum or pattern), a completed Flow has no re-reply surface, so the channel
**re-sends a fresh Flow** for the same interaction: same flow token, the cached
flow id, and a body that repeats the question with the door's error line (which
names the failing field). This is **bounded** — after a fixed number of
rejections the channel stops re-sending, tells the guest once the form could not
be processed, and lets the ask time out on its own deadline. The platform
validates the answer schema against this subset when the question is asked —
before the question is stored — so an out-of-subset schema (nested objects,
arrays, unions, unknown types) never reaches this send; the Flow mapping rejects
one defensively too.
Correlation is fully out-of-band: any reply (typed, tapped, or
a submitted form) from the recipient resolves the `(phone_number_id, wa_id)`
pair's pending question, so one question can be pending per pair at a time; a
second concurrent one is rejected loudly.

An agent notification (`notify_user`) advertises the full capability set —
`supports_media_notifications`, `supports_location_notifications`,
`supports_template_notifications`, `supports_interactive_notifications`, and
`supports_form_notifications` — so a notification may carry the full outbound
vocabulary:

- **Media** — each file item sends as its own native message: an `image`,
  `document` (with `caption`/`filename`), `video` (with `caption`), or `audio`
  (voice/clip; the Cloud API audio object carries no caption/filename); a `link`
  item is appended to the body text as a `label: url` line.
- **Options** (`list[Option]`, a typed union, never bare strings) — a
  `ReplyOption` renders as a native reply **button** (or a **list** row past the
  button caps / for a described option), sending its **authored `id`** on the
  wire when set (echoed back on tap) else a minted index; a tap enters the
  conversation as a visitor message. A lone `LinkOption` renders as a `cta_url`
  interactive (one URL button); mixed reply+link or multiple links append the
  link(s) to the body as `label: url` lines (WhatsApp has no multi-URL button).
- **Sections** (`list[OptionSection]`) — a multi-section interactive **list**,
  each row carrying an optional **description** secondary line.
- **Header + footer** — a media `header` (image/video/document) rides the
  interactive header (an `audio` header is sent as its own message ahead of it);
  a text `footer` rides the interactive footer. Both require options/sections.
- **Location** (`LocationElement`) — a native location message (a pin with an
  optional name/address).
- **Template** — for a send outside the 24-hour window, a pre-approved
  `ChannelTemplate` by name: its `header_media` (image/video/document),
  `body_parameters` (positional body-text fills), and `buttons` (per-button
  quick-reply `payload` / url suffix) map onto the template-message components.
- **Ask-less form** (`schema`) — an in-chat WhatsApp Flow (see below).

`template`, a choice surface, and `schema` are mutually exclusive on one
notification; `options` and `sections` are mutually exclusive with each other;
media and location may combine with a choice surface.

An **ask-less form** notification renders exactly like a form ask — any media
first, then a WhatsApp Flow whose body is the message — but stores **no
correlation**: the flow token is minted in the `tai42-nf:` namespace
(`tai42-nf:{schema hash}:{random}`), and the inbound webhook routes an
`nfm_reply` by that prefix *before* any pending-question lookup, so a submission
enters the conversation as a structured guest message (rendered `label: value`
text plus the structured copy) and can never answer — or disturb — a question
pending on the same pair. The answer schema is cached durably beside the
published-flow id under the schema hash; a reply carries only the hash, so a
lost cache entry cannot be repopulated from it — the reply then degrades to its
raw (uncoerced) values, and is still accepted. Like every freeform send, a form
notification delivers only inside Meta's 24-hour customer-service window — out
of window the send fails loudly (error 131047), synchronously or as a `failed`
status; it is never silently downgraded to a template.

A `confirm` or `external` question arrives as a tappable link and is answered in
the browser via the callback door — no WhatsApp reply is expected or matched, and
it never consumes the pair.

## Inbound entry parameters

When an inbound message enters the conversation **bridge** as a fresh turn (a
message with no pending question to answer), the channel forwards a set of opaque
**entry parameters** alongside the turn text. They ride verbatim to a `tool`
target's payload under its own `params` key (`payload["params"]`) — the platform
attaches **no meaning and no trust**; a channel-agnostic consumer opts into
whichever keys it understands. This is the channel's public inbound contract:

| `params` key | Set when | Value |
|---|---|---|
| `reply_id` | An interactive reply button / list row is tapped but is **not** an answer to a pending ask (no ask, or a stale/other id) | The question-bound wire id the outbound carried (`button_reply.id` / `list_reply.id`) |
| `reply_description` | The tapped item is a **list row** that carried a secondary description line | `list_reply.description` |
| `button_payload` | A **template quick-reply** button is tapped (a `button`-type message) | The developer-defined `button.payload` behind the visible `button.text` |
| `context_message_id` | The message **quotes / replies to** an earlier one | `context.id` (the quoted `wamid`) |
| `referral_source_url` | The message enters via **click-to-WhatsApp / QR** (`referral`) | `referral.source_url` |
| `referral_source_id` | " | `referral.source_id` |
| `referral_source_type` | " | `referral.source_type` (e.g. `ad`, `post`) |
| `referral_ctwa_clid` | " | `referral.ctwa_clid` (the click-to-WhatsApp click id) |
| `referral_headline` | " (when present) | `referral.headline` |
| `referral_body` | " (when present) | `referral.body` |
| `media_kind` | An inbound **media** message (image/document/audio/video/sticker) | The wire type |
| `media_id` | " | The Graph media id (re-fetch handle — see the design note below) |
| `media_mime_type` | " | The media object's `mime_type` |
| `media_sha256` | " | The media object's `sha256` (content integrity) |
| `media_filename` | An inbound **document** | `document.filename` |
| `media_voice` | An inbound **audio** that is a voice note | `"true"` (`audio.voice`) |
| `sticker_animated` | An animated **sticker** | `"true"` (`sticker.animated`) |
| `reaction_emoji` | An inbound **reaction** (absent = a removed reaction) | `reaction.emoji` |
| `reaction_message_id` | An inbound **reaction** | The `wamid` it was applied to (`reaction.message_id`) |
| `contacts_count` | An inbound **contacts** message | The number of shared contact cards |
| `contacts` | " | The raw `contacts` array as compact JSON (dropped when over the per-value cap; `contacts_count` still rides) |

Guest **media** and **location** and **reactions** and **contacts** now bridge
as turns (previously inbound-dropped). The caption of a media message becomes the
turn text (a faithful `[image]` / `[document: file]` / `[voice message]` / … 
placeholder when caption-less); an inbound **location** lands as a typed
`LocationElement` on the turn's `location` (a machine-consumable field, not a
param), with the place name/coordinates as the text.

> **Inbound media design note (design gap).** WhatsApp inbound media arrives as a
> Graph media **id**; resolving it to bytes is a two-step Graph call that returns a
> **short-lived, Bearer-authenticated** lookaside URL — not a durable public
> `https` URL and not a valid `MediaItem` source. Minting a durable **typed
> `attachments`** entry needs a served-media **ingestion** seam (fetch → persist →
> mint a `{MEDIA_ROUTE_PREFIX}{id}` served reference); the platform's served-media
> store is not reachable from a channel plugin and handles only outbound
> `data:image` substitution, so no such seam exists today. Rather than invent
> infrastructure or fabricate an unfetchable URL, inbound media therefore bridges
> **without** a typed `attachments` entry — its identity rides the `media_*` params
> above and a consumer re-fetches via `media_id` with operator credentials. The
> durable fix is a platform served-media ingestion seam on the app handle.

Params ride **only on the bridge path**. A tap or quick-reply that **answers** a
pending question does not surface them: the answer path forwards `{"answer": …}`
to the callback door — a seam that carries no params — and a tap's id is already
consumed there to select the option. Values are transport-bounded (per the
platform's entry-param limits — count, key charset, value length, total size); an
individual value over the per-value cap is dropped (never truncated), and in the
rare case the aggregate still overflows a bound the whole set is dropped and the
turn bridges without it — a guest message is never lost to a params bound.

Meta inbound **error notices** (e.g. an unsupported message type the guest sent,
carried as an `errors[]` array) are logged at **warning** with the detail and are
never bridged as guest turns.

## Delivery statuses

WhatsApp reports delivery asynchronously: a send returns `2xx` with a `wamid`, and
a later `statuses` webhook carries `sent`/`delivered`/`read`/`failed`. The same
`POST` endpoint records those receipts through the interactions facet — `failed`
(e.g. a message outside the 24-hour session window, error 131047) marks the
answer failed loudly; `sent`/`delivered` confirm it; `read` is informational and
ignored.

A `wamid` the conversation bridge does not own is not a dead end: it may be a
**flow send** (`notify_user`, which runs no conversation record). The webhook then
resolves the `wamid` through the send-outcome receipt index and, on a hit, posts a
`delivery_receipt` event onto the send's originating monitoring trace, nested under
its send span — `ERROR` for a `failed` receipt (carrying the provider `errors`),
informational otherwise. This is observability only: it annotates the flow's trace,
never re-sending or failing the flow. Only a genuine miss — neither the bridge nor
any flow send owns the `wamid` — keeps the untracked-message log; either way the
status is acknowledged, never retried.

## Security

- Inbound requests authenticate via `X-Hub-Signature-256`:
  `sha256=` + hex(HMAC-SHA256(app_secret, raw body)), validated **fail-closed**
  with a constant-time compare **before the body is parsed**. A missing/empty app
  secret is an operator error that raises loudly (logged 500) — never a soft 401
  that reads like a bad signature.
- The GET verification handshake echoes `hub.challenge` only when
  `hub.verify_token` matches the configured token under a constant-time compare.
- Meta's signature scheme carries no timestamp, so a captured request validates
  forever; the `wamid` dedupe window (48h default) plus HTTPS are the replay
  guards.
- The access token, app secret, and verify token are `SecretStr` — never in a
  repr, log line, or traceback; the plaintext is read only at the Bearer-auth and
  HMAC seams.
- The unauthenticated route bounds its body read (1 MiB → 413) before any
  signature work.

## Limits

| Limit | Consequence |
|---|---|
| One pending question per `(phone_number_id, wa_id)` pair | A second concurrent `ask_user` over this channel fails loudly with `PendingQuestionExistsError` while the first is unanswered/unexpired |
| Freeform sends need the 24h window | A freeform send (question, reply, media) outside the human's 24-hour session window is rejected by Meta (error 131047), synchronously as a delivery error or asynchronously as a `failed` status. A template is the only send Meta accepts outside the window |
| Single send attempt per part | A transient Cloud API outage fails the send instead of retrying (no idempotency key → a blind retry risks double-messaging). A multi-part media send that fails on the Nth part raises naming the wamids already delivered |
| Inbound media carries no typed attachment | Inbound **media** (image/document/audio/video/sticker) bridges as a turn (caption → text, identity → `media_*` params) but **without** a typed `attachments` entry: the Graph media id is not a durable `MediaItem` source and no served-media ingestion seam is reachable from a channel (see the inbound media design note). A consumer re-fetches bytes via `media_id` + operator credentials. Inbound **location** DOES land a typed `LocationElement`; **contacts**/**reactions** ride `params` |
| Form schema is a flat object subset | A `form` ask's answer schema is a top-level `object` whose properties are `string`, `string`+`enum`, `boolean`, `integer`, or `number`. The platform enforces this subset at ask-time, so nested objects, arrays, and `oneOf`/`anyOf` are refused before the question is stored; the Flow mapping refuses them defensively too, and additionally rejects a property named `flow_token` — Meta reserves that key on the Flow response, so a field of that name is unanswerable on this channel |
| Template audio header unsupported | A `ChannelTemplate` maps `header_media` (image/video/document), `body_parameters`, and quick-reply/url `buttons` onto the template-message components. An **audio** `header_media` has no Cloud API template representation and is refused loudly (`ChannelInputError`); an audio interactive **header** on a notification is instead sent as its own message ahead of the interactive |
| No timestamp in Meta's signature scheme | Replay of a captured request validates forever for that body; `wamid` dedupe (48h default window) + HTTPS are the guards (a Meta protocol property) |

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

The live integration suite (`pytest -m integration`) sends real messages via the
WhatsApp API and runs only when the `CHANNEL_WHATSAPP_*` credentials
are present in the environment; it skips cleanly otherwise.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

## Correlation surface (2.0)

Since 2.0 inbound answers resolve through the platform's shared inbound-answer
ladder: the plugin exposes its correlation store over the contract's
`CorrelationStore` port (reserve / peek / release) plus a transport ack, and the
skeleton owns the forward / retry-in-place / bridge ladder. The plugin-local
`pop`/`restore` correlation helpers from 1.x are gone.
