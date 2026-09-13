# tai42-channel-slack

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Slack `channel` plugin for the TAI ecosystem. `ask_user(..., channel="slack")`
posts the question to a configured Slack channel via `chat.postMessage`; the
human replies **in the message's thread**; the Slack Events API delivers the
reply to this plugin's inbound door, which verifies the request signature and
forwards the typed answer to the interaction's public callback URL — so a tool
blocked on a question resumes with a real human answer, out-of-band.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `channel`
is a registered deliverer that pushes an interaction question to a human on a
specific medium and bridges the reply back into the interactions store. This
package is one such deliverer (Slack); siblings back the same contract with
Telegram or Twilio (SMS/WhatsApp). The ecosystem is open-ended: any package can
back the same contract, so this repo is this channel's own full doc home, and
the documentation site covers the platform-level story:

- Interactions concept: https://tai42.ai/concepts/interactions
- Build a channel plugin (author guide): https://tai42.ai/guides/authors/channel
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Channel` protocol,
`ChannelDelivery`, `ChannelDeliveryError`, and the `tai42_app` handle) and
`tai42-kit[redis]` (`HttpxClient`, `RedisClient`, `TaiBaseSettings`, and the
settings cache). Beyond those it depends on `httpx`, `starlette`, and
`pydantic` / `pydantic-settings` — the whole Slack surface is two HTTPS POSTs
plus stdlib `hmac`; no `slack_sdk`, no Bolt.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-channel-slack
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/channel-slack
```

## Discovery

The skeleton discovers this plugin through the manifest's `channel_modules`
field — at app load it imports every module under each named package, and
`tai42_channel_slack.register` registers the `"slack"` channel, the Events API
inbound route `POST /api/channels/slack/inbound`, and the interactivity route
`POST /api/channels/slack/interactive` as a side-effect. A bare
`import tai42_channel_slack` registers nothing (library use).

```yaml
channel_modules: [tai42_channel_slack]
```

`ask_user(..., channel="slack")` then selects it by name.

## Configuration

Settings are read from the `CHANNEL_SLACK_` environment group (see
`SlackSettings` / `SlackRedisSettings`). Credentials are operator-bound
environment configuration — never LLM-visible tool parameters. The recipient
is resolved per question: a caller may request one (`ask_user(...,
recipient=...)`), and the plugin sends to it only if it is on the operator
allowlist — an unlisted recipient fails loudly, nothing is sent; a question
without a requested recipient goes to the operator default.

| Env var | Meaning |
| --- | --- |
| `CHANNEL_SLACK_BOT_TOKEN` | bot token, `xoxb-…`, scope `chat:write` (SecretStr) |
| `CHANNEL_SLACK_SIGNING_SECRET` | Events API signing secret (SecretStr) |
| `CHANNEL_SLACK_ALLOWED_RECIPIENTS` | whitelist of channel/DM ids a caller may request — comma-separated or a JSON list; empty rejects every requested recipient |
| `CHANNEL_SLACK_DEFAULT_RECIPIENT` | channel/DM id (`C…`/`D…`) used when the caller requests none |
| `CHANNEL_SLACK_REDIS_URL` | correlation store, e.g. `redis://redis:6379/0` |
| `CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS` | outbound HTTP budget, default `30` |

All credential/target fields default to unset, so importing the package never
demands configuration; a delivery or inbound event against an unconfigured
channel raises loudly, naming the missing env var. An unset or empty signing
secret fails CLOSED — the inbound door raises rather than ever verifying
against a forgeable key.

## Slack app setup

All links: https://docs.slack.dev

1. Create a Slack app; install it to the workspace with bot scope `chat:write`
   (plus `channels:history` for a public channel target or `im:history` for a
   DM target); copy the bot token into `CHANNEL_SLACK_BOT_TOKEN`.
2. Copy the app's signing secret into `CHANNEL_SLACK_SIGNING_SECRET`.
3. Event Subscriptions → enable, Request URL =
   `{INTERACTIONS_PUBLIC_BASE_URL}/api/channels/slack/inbound` (the app must
   already be running so the signed `url_verification` handshake succeeds),
   and subscribe to bot events `message.channels` (and/or `message.im`).
4. Interactivity & Shortcuts → enable, Request URL =
   `{INTERACTIONS_PUBLIC_BASE_URL}/api/channels/slack/interactive` — required
   for `form` questions, whose modal is opened and submitted through this door
   (same signing secret, verified the same way).
5. Invite the bot to every target channel; put the fallback channel id in
   `CHANNEL_SLACK_DEFAULT_RECIPIENT` and list the ids callers may request in
   `CHANNEL_SLACK_ALLOWED_RECIPIENTS`.
6. There is no boot-time registration call — the dashboard Request URLs are the
   registration.

## How an answer travels

1. A tool calls `ask_user(question, channel="slack", ...)`. The runtime
   persists the interaction, mints a single-use callback ticket, and hands the
   plugin a `ChannelDelivery`.
2. `SlackChannel.deliver` resolves the recipient — the caller-requested id if
   it is on `CHANNEL_SLACK_ALLOWED_RECIPIENTS` (unlisted refuses loudly,
   nothing sent), else `CHANNEL_SLACK_DEFAULT_RECIPIENT` — and posts the
   question there via `chat.postMessage`. Success is the JSON `ok: true` field — Slack answers
   HTTP 200 even for failed sends, so the status code is never the signal; any
   failure raises `ChannelDeliveryError` loudly. One send attempt, no retry
   (`chat.postMessage` offers no idempotency key).
3. For `text` and `select` questions the message says to reply **in the
   thread** and shows the deadline; the response's `ts` is stored in Redis as
   `ts → callback_url` with TTL = the question's remaining budget. For
   `confirm` and `external` questions the callback URL rides the message as a
   plain link instead — the human answers through the callback door directly,
   and no correlation state exists. For `form` questions the message carries a
   **Fill form** button; before it is sent, a form record (`{callback_url,
   schema, question, timeout_at}`) is stored in Redis under
   `channel:slack:form:{interaction_id}` with TTL = the remaining budget
   (released if the send fails).
4. **`form` questions.** The button click reaches the interactivity door
   `POST /api/channels/slack/interactive` as a `block_actions` payload; the door
   verifies the signature, peeks the form record, and opens a Block Kit modal
   (`views.open`) built from the schema — one input per property (`string` →
   text/select, `boolean` → yes/no, `integer`/`number` → number). On submit, a
   `view_submission` payload arrives; the door coerces the state to the schema's
   JSON types and forwards `{"answer": {...}}` to the stored callback URL. A door
   `2xx` closes the modal and drops the record; a `400` shows the door's message
   on the form (record kept); a `404` shows an expired notice (record dropped).
5. The human replies in the thread. The Slack Events API POSTs the event to
   `POST /api/channels/slack/inbound`, which reads a bounded body, verifies
   the `X-Slack-Signature` v0 HMAC over the raw bytes (constant-time,
   ±300 s replay window, fail-closed), dedupes on `event_id`, matches the
   reply's `thread_ts` against the correlation store, and forwards
   `{"answer": "<typed text>"}` to the stored callback URL.
6. The public callback door validates the answer against the question's stored
   format and records it; the blocked `ask_user` returns it.

Operational notes: answers must be typed in-thread; non-answer traffic (edits,
bot echoes, other channels, top-level messages, threads with no pending
question) is always acked 2xx so Slack never disables the event subscription;
the question message shows the deadline, and the waiting ask surfaces an
unanswered question through its own timeout.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

The live integration suite (`pytest -m integration`) posts a real message to
the configured workspace; it skips cleanly when the `CHANNEL_SLACK_*`
credentials are absent.

Deferred by design: Socket Mode inbound (needs a managed background task —
platform scope; webhook mode needs exactly the public URL the external
interactions flow already requires) — the plugin runs in webhook mode only.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

## Correlation surface (2.0)

Since 2.0 inbound answers resolve through the platform's shared inbound-answer
ladder: the plugin exposes its correlation store over the contract's
`CorrelationStore` port (reserve / peek / release) plus a transport ack, and the
skeleton owns the forward / retry-in-place / bridge ladder. The plugin-local
`pop`/`restore` correlation helpers from 1.x are gone.
