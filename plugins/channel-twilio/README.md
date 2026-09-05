# tai42-channel-twilio

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Twilio SMS/WhatsApp **channel** plugin for the TAI ecosystem. It delivers an
`ask_user` question to a human's phone through the Twilio Messages API and
bridges the human's reply back into the interactions store — so an agent can
reach a person out-of-band instead of only showing the question in the Studio
inbox. It implements the `tai42_contract.channels.Channel` protocol and registers
under the name `"twilio"`.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Channel`
is "how a question reaches a human" — a pluggable deliverer the runtime resolves
by name when `ask_user` is called with `channel=...`. This package is one such
deliverer (Twilio SMS/WhatsApp); siblings back the same contract with Telegram
or Slack. The ecosystem is open-ended: any package can back the same contract,
so this repo is this plugin's own full doc home, and the documentation site
covers the platform-level story:

- Interactions concept: https://tai42.ai/concepts/interactions
- Build a channel plugin (author guide): https://tai42.ai/guides/authors/channel
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Channel` protocol,
`ChannelDelivery`, `ChannelDeliveryError`, and the `tai42_app` handle) and
`tai42-kit[redis]` (`HttpxClient`, `RedisClient`, `TaiBaseSettings`, and the
settings cache). Beyond those it depends on `httpx`, `starlette`, and
`pydantic` / `pydantic-settings`. There is **no Twilio SDK**: the send is one Basic-auth form POST over
`httpx`, and webhook signature validation is ~30 lines of stdlib
`hmac`/`hashlib`/`base64`.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-channel-twilio
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/channel-twilio
```

## Discovery

The runtime discovers this plugin through the manifest's `channel_modules` key:

```yaml
channel_modules: ["tai42_channel_twilio"]
```

At app load the runtime imports every module under the package, and
`register.py` fires the registrations as its import side-effect: the `"twilio"`
channel on `tai42_app.channels`, and — via the `inbound` import — the
unauthenticated webhook route on `tai42_app.http`. A bare
`import tai42_channel_twilio` registers **nothing** — the package is library-safe;
only the register module carries the side-effect.

## Configuration

Settings are read from the `CHANNEL_TWILIO_` environment group (see
`TwilioSettings` / `TwilioRedisSettings`):

| Env var | Required | Meaning |
|---|---|---|
| `CHANNEL_TWILIO_ACCOUNT_SID` | yes | Twilio Account SID (Basic-auth username, URL path segment) |
| `CHANNEL_TWILIO_AUTH_TOKEN` | yes | Auth token (`SecretStr`) — Basic-auth password AND webhook signature HMAC key |
| `CHANNEL_TWILIO_FROM` | yes | The deployment's Twilio number (E.164, or `whatsapp:+...`) |
| `CHANNEL_TWILIO_DEFAULT_RECIPIENT` | for recipient-less asks | Destination number used when the caller requests no recipient (E.164, or `whatsapp:+...`) |
| `CHANNEL_TWILIO_ALLOWED_RECIPIENTS` | for caller-requested recipients | Whitelist a caller-requested recipient must be on — comma-separated or a JSON list; an unlisted request is refused loudly |
| `CHANNEL_TWILIO_REDIS_URL` | yes | Correlation store (plugin-owned Redis) |
| `CHANNEL_TWILIO_REDIS_MAX_CONNECTIONS` … | no | The rest of the kit `RedisConnectionSettings` fields, same names under this prefix |
| `CHANNEL_TWILIO_HTTP_TIMEOUT_SECONDS` | no (30.0) | Outbound send + answer-forward timeout, seconds |
| `CHANNEL_TWILIO_DEDUPE_TTL` | no (172800) | Seen-MessageSid replay-guard window, seconds |

Recipient policy is operator-owned: a caller may request a recipient per ask,
but it is sent to only if it is on `CHANNEL_TWILIO_ALLOWED_RECIPIENTS` (fail
closed — an unlisted recipient raises, nothing is sent); with no requested
recipient the ask goes to `CHANNEL_TWILIO_DEFAULT_RECIPIENT`, which is trusted
without an allowlist check. Secrets live only in the environment.

Two steps happen **out-of-band** (the plugin never mutates Twilio account
configuration at startup):

1. Point the Twilio number's *"A message comes in"* webhook at
   `{public base URL}/api/channels/twilio/inbound`, HTTP POST (Twilio console
   or REST API).
2. Deploy behind a TLS-terminating proxy you control that sets
   `X-Forwarded-Proto` / `X-Forwarded-Host` — the webhook signature is
   validated against the public URL reconstructed from those headers, so it
   always tracks what Twilio actually called.

## How a human answers

A `text` or `select` question arrives as a normal SMS (a select ask lists its
options numbered). The human **just replies** — no code to quote, no prefix.
Correlation is fully out-of-band: any reply from the configured number resolves
the pair's pending question, so one question can be pending per number pair at
a time; a second concurrent one is rejected loudly.

A `confirm` or `external` question arrives as a tappable link and is answered
in the browser via the callback door — no SMS reply is expected or matched, and
it never consumes the number pair.

## Media and the message vocabulary (the medium's honest ceiling)

Both `deliver` (questions) and `notify` (fire-and-forget) support **display
media**, degraded honestly per kind to this text-first medium — so this channel
advertises `supports_media_notifications`:

| Media kind | SMS/MMS rendering |
|---|---|
| `image` / `video` / `audio` | A repeated Twilio `MediaUrl` attachment (a public `https` url Twilio fetches; the carrier applies its own per-type fallback). An inline `data:` image is refused with `ChannelInputError` — Twilio fetches a url, not inline bytes. The item's caption is alt-text a `MediaUrl` cannot carry, so it is dropped rather than leaked into the body. |
| `document` | A `filename: url` body line (its suggested download name — which a `MediaUrl` cannot carry — followed by the link; `caption` then bare `url` are the fallbacks). MMS document rendering is carrier-unreliable, so a named tappable link is the honest degradation. |
| `link` | A `caption: url` body line, or the bare `url` when no caption rides (SMS has no rich link cards; the human taps through). |

A **content-only** notify (blank message) sends its media alone: an image/video/audio-only
send goes out as a **body-less MMS** (the `Body` field is omitted, only the `MediaUrl`
rides — Twilio accepts that when media is present); a document/link-only send renders that
labelled line **as** the body.

### What does NOT reach this channel (honest reachability)

The new message vocabulary adds tappable options, sectioned option lists, shared
locations, pre-approved templates, fillable forms, and interactive header/footer
enhancements. **SMS/MMS supports none of them natively** — no tappable buttons, no map
pin, no template component model, no form. So this channel advertises **only**
`supports_media_notifications` and declares none of
`supports_interactive_notifications` / `supports_location_notifications` /
`supports_template_notifications` / `supports_form_notifications` /
`supports_form_delivery`. The platform's central capability guard (`notify_user` and the
conversations delivery executor) reads these flags and **refuses** a message carrying an
unsupported shape *before it ever reaches this channel* — rather than letting the channel
fake an affordance or silently drop the content:

| Shape | Reaches this channel? | Why |
|---|---|---|
| `media` (image/video/audio/document/link) | **yes** | `supports_media_notifications = True`; rendered as above |
| `options` (flat `ReplyOption`/`LinkOption`) | no | needs `supports_interactive_notifications` — refused upstream |
| `sections` (titled `ReplyOption` groups) | no | needs `supports_interactive_notifications` — refused upstream |
| `header` / `footer` | no | ride an interactive surface (options/sections), so refused transitively |
| `location` | no | needs `supports_location_notifications` — refused upstream |
| `template` (`ChannelTemplate`) | no | needs `supports_template_notifications` — refused upstream (see below) |
| `schema` (ask-less form) | no | needs `supports_form_notifications` — refused upstream |
| `form` delivery (an `ask_user` form) | no | needs `supports_form_delivery` — refused upstream |

**Templates.** This channel does not use Twilio's Content API / `ContentSid` template
sends today — every outbound is a freeform `Body` (plus any `MediaUrl`). It therefore does
not advertise `supports_template_notifications`, so a `ChannelTemplate` is refused before it
reaches here, keeping that behaviour coherent (approved-template sends over a `whatsapp:`
number are the dedicated `channel-whatsapp` plugin's job).

A `select` **ask** (the `deliver` path, whose `options` are a plain `list[str]`, not the
notify vocabulary) still renders its options as numbered body lines, and the human answers
by **typing** one option — the inbound webhook forwards the typed reply verbatim to the
callback door, which validates it against the option set. That typed-reply mapping is a
real answer path and the medium's honest ceiling for choice input; there is no button-tap
or reply-by-number machinery. Because an inbound SMS matches purely by its **text**, an
author-set option **id** can never round-trip over this channel — it is dropped, never
echoed back on the reply.

## Security

- Inbound requests authenticate via `X-Twilio-Signature`:
  `base64(HMAC-SHA1(auth_token, public_url + sorted form pairs))`, validated
  **fail-closed** with a constant-time compare before one byte of the body is
  trusted. A missing/empty auth token is an operator error that raises loudly —
  never a soft 401 that reads like a bad signature.
- Twilio's signature scheme carries no timestamp, so a captured request
  validates forever; the `MessageSid` dedupe window (48h default) plus HTTPS
  are the replay guards.
- The auth token is a `SecretStr` — it never surfaces in a repr, log line, or
  traceback; the plaintext is read only at the Basic-auth and HMAC seams.
- The unauthenticated route bounds its body read (1 MiB → 413) before any
  signature work.

## v1 limits

| Limit | Consequence | Future |
|---|---|---|
| Recipients fixed by operator env (`_ALLOWED_RECIPIENTS`/`_DEFAULT_RECIPIENT`) | A question can only reach a whitelisted or default number — no dynamic/unlisted destinations | Directory-backed recipient resolution |
| One pending question per number pair | A second concurrent `ask_user` over this channel fails loudly with `PendingQuestionExistsError` while the first is unanswered/unexpired | Number pool via Messaging Service sticky sender |
| SMS-first; WhatsApp freeform only | With `whatsapp:` numbers, a question outside the human's 24h session window is rejected by Twilio (error 63016) as a loud delivery failure | Approved-template (`ContentSid`) sends |
| Single send attempt | A transient Twilio outage fails the ask instead of retrying (no idempotency key exists → a blind retry risks double-texting) | App-side dedupe + retry |
| Flow-send delivery receipts are observability only | The Twilio StatusCallback closes the loop on both legs: a **bridge** outbound's `delivered`/`failed`/`undelivered` flips the answer record (a late failure ends it `failed`, not just the ask's timeout), and a **flow send** (`notify_user`, which runs no bridge record) has its receipt resolved through the send-outcome index and posted as a `delivery_receipt` event on the originating flow trace (`ERROR` on failure). The flow-send receipt is a monitoring signal only — it never re-sends or fails the flow — and correlates only when a trace was ambient at the send and the interactions store is configured; a status neither leg owns is acked and logged | — |
| No timestamp in Twilio's signature scheme | Replay of a captured request validates forever for that URL+body; MessageSid dedupe (48h default window) + HTTPS are the guards | — (Twilio protocol property) |

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

The live integration suite (`pytest -m integration`) sends real messages via
the Twilio API and runs only when the `CHANNEL_TWILIO_*` credentials are
present in the environment; it skips cleanly otherwise.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

## Correlation surface (2.0)

Since 2.0 inbound answers resolve through the platform's shared inbound-answer
ladder: the plugin exposes its correlation store over the contract's
`CorrelationStore` port (reserve / peek / release) plus a transport ack, and the
skeleton owns the forward / retry-in-place / bridge ladder. The plugin-local
`pop`/`restore` correlation helpers from 1.x are gone.
