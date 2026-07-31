# tai42-channel-telegram

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Telegram `Channel` plugin for the TAI ecosystem. It delivers an `ask_user`
question to a Telegram chat as a `sendMessage` — the caller's requested
recipient if it is on the operator allowlist, otherwise the operator-configured
default chat — a
ForceReply for typed `text`/`select` answers, a tappable URL button opening the
interaction callback door for `confirm`/`external` — and bridges the human's
typed reply back to the interactions store through its own verified webhook
route. Outbound is plain HTTPS over a pooled `httpx` client; there is no
Telegram SDK dependency (the Bot API is flat JSON-over-HTTPS).

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Channel`
is a registered deliverer that pushes an interaction question to a human on a
specific medium and bridges the reply back into the interactions store — so
`ask_user` can reach a person out-of-band instead of only showing the question
in the Studio inbox. This package is one such channel (Telegram); siblings back
the same contract with Slack or Twilio SMS/WhatsApp. The ecosystem is
open-ended: any package can back the same contract, so this repo is this
channel's own full doc home, and the documentation site covers the
platform-level story:

- Interactions concept: https://tai42.ai/concepts/interactions
- Build a channel plugin (author guide): https://tai42.ai/guides/authors/channel
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Channel` protocol,
`ChannelDelivery`, `ChannelDeliveryError`, and the `tai42_app` handle) and
`tai42-kit[redis]` (`HttpxClient`, `RedisClient`, `TaiBaseSettings`, and the
settings cache). Beyond those it depends on `httpx`, `starlette`, and
`pydantic` / `pydantic-settings`.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-channel-telegram
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/channel-telegram
```

## Discovery

The skeleton discovers this channel by **importing its modules** — the
manifest's `channel_modules` loader imports every module under the package, and
importing `tai42_channel_telegram.register` fires the registrations as a
side-effect: the `"telegram"` channel name on `tai42_app.channels`, the public
inbound route, and the `setWebhook` startup hook. Name the package in your
manifest:

```yaml
channel_modules:
  - tai42_channel_telegram
```

A bare `import tai42_channel_telegram` (library use) does NOT register anything.

## Configuration

Settings are read from the `CHANNEL_TELEGRAM_` environment group (see
`TelegramSettings`). Every credential is bound to env by the operator — never a
tool parameter, never visible to the LLM:

| Env var | Default | Purpose |
| --- | --- | --- |
| `CHANNEL_TELEGRAM_BOT_TOKEN` | — | Bot credential from BotFather (required) |
| `CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS` | `[]` | Whitelist of chats a caller-supplied recipient may name (numeric id — negative for groups — or `@username`), as a comma-separated string or a JSON list |
| `CHANNEL_TELEGRAM_DEFAULT_RECIPIENT` | — | The chat questions go to when the caller names no recipient (trusted; not checked against the allowlist) |
| `CHANNEL_TELEGRAM_WEBHOOK_SECRET` | — | `setWebhook` secret_token; verified on every inbound update (required) |
| `CHANNEL_TELEGRAM_PUBLIC_BASE_URL` | — | This deployment's public base URL (required) |
| `CHANNEL_TELEGRAM_REDIS_URL` | — | Redis the correlation store lives in (required) |
| `CHANNEL_TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Bot API origin (stub servers/e2e only) |
| `CHANNEL_TELEGRAM_HTTP_TIMEOUT_SECONDS` | `30` | Budget per outbound HTTP call |

Optional Redis connection tuning (see `TelegramCorrelationSettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `CHANNEL_TELEGRAM_REDIS_MAX_CONNECTIONS` | — | Pool size cap |
| `CHANNEL_TELEGRAM_SOCKET_TIMEOUT` | — | Per-command socket timeout (seconds) |
| `CHANNEL_TELEGRAM_SOCKET_CONNECT_TIMEOUT` | — | Connect-phase timeout (seconds) |
| `CHANNEL_TELEGRAM_RETRY_ON_TIMEOUT` | `false` | Retry commands on timeout |
| `CHANNEL_TELEGRAM_RETRY_ATTEMPTS` | `0` | Exponential-backoff retries on connection errors |

## How an answer travels

1. A tool calls `ask_user(question, channel="telegram", ...)`. The runtime
   persists the interaction, mints a public callback ticket, and calls this
   plugin's `deliver` with the question and its `callback_url`.
2. `deliver` resolves the recipient chat: a caller-supplied recipient must be
   on `CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS` or the delivery is refused (fail
   closed, nothing sent); no caller recipient means
   `CHANNEL_TELEGRAM_DEFAULT_RECIPIENT`. It then sends ONE `sendMessage` to
   that chat — the Bot API has
   no idempotency key, so a failed send raises `ChannelDeliveryError` instead
   of retrying (a blind retry could double-send).
   - **`text` / `select` (typed reply):** the message carries
     `reply_markup: {force_reply: true}`, and the sent `message_id →
     callback_url` mapping is stored in plugin-owned Redis keys
     (`channel:telegram:corr:{message_id}`, TTL = the question's remaining
     budget). A `select` question lists its options as guided text.
   - **`confirm` / `external` (tap):** the message carries a tappable URL
     button opening the callback door directly — no correlation state, no
     inbound involvement. (`form` has no single-reply mapping and is rejected
     before delivery.)
3. The human replies in Telegram. Telegram POSTs the update to this plugin's
   own public route `POST /api/channels/telegram/inbound`, registered by
   `setWebhook` at startup with a shared `secret_token`.
4. The inbound door verifies `X-Telegram-Bot-Api-Secret-Token` against the
   configured secret — constant-time over sha256 digests of both sides, and
   FAIL CLOSED: an unset or empty secret answers 500, never "skip
   verification". The body is read through a streaming bounded reader (413 the
   moment it crosses 1 MiB). The reply is matched on
   `message.reply_to_message.message_id` and the configured recipient chats
   (the default recipient plus the allowlist; an entry matches the update's
   numeric chat id or its `@username`), the
   callback_url is looked up in the correlation store, and the answer is
   forwarded as the JSON object `{"answer": "<typed text>"}` — the callback
   door validates it against the question's stored `answer_format` and records
   it.
5. The blocked `ask_user` call returns the recorded answer.

Telegram redelivers an update until it gets a 2xx, so the inbound status code
is the retry contract:

| Condition | Response |
| --- | --- |
| Configured secret unset/empty, or no recipient chat configured | 500 (fail closed, logged) |
| Header missing or mismatched | 401 (one constant deny body) |
| Body over the cap / not a JSON object | 413 / 400 |
| Update out of scope (no message / chat not a configured recipient / not a reply / no text) | 200 "ignored" (reason logged) |
| No pending correlation for the replied-to message | 200 "ignored" (logged warning) |
| Callback door 200 | 200 "forwarded", mapping cleared |
| Callback door 404 (ticket terminally gone) | 200 "stale", mapping cleared |
| Callback door 400 (answer rejected) | 200 "rejected", mapping KEPT — the human can reply again |
| Callback door other status / unreachable | exception → 500, Telegram's redelivery is the recovery |

### Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and put
   its token in `CHANNEL_TELEGRAM_BOT_TOKEN`.
2. Find each target chat id (message the bot, then read
   `https://api.telegram.org/bot<token>/getUpdates`; group ids are negative).
   Put the fallback chat in `CHANNEL_TELEGRAM_DEFAULT_RECIPIENT` and any chats
   callers may address per ask in `CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS`
   (comma-separated or a JSON list).
3. Generate a webhook secret (1-256 chars of `[A-Za-z0-9_-]`) for
   `CHANNEL_TELEGRAM_WEBHOOK_SECRET`.
4. Set `CHANNEL_TELEGRAM_PUBLIC_BASE_URL` to the deployment's public origin.
   Telegram only delivers webhooks over **HTTPS** (TLS ≥ 1.2) to ports
   **443, 80, 88, or 8443**. The startup hook points the bot's webhook at
   `{public_base_url}/api/channels/telegram/inbound` and aborts startup loudly
   if `setWebhook` fails.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

The offline suite is fully self-contained (fake transport, in-memory Redis).
The live suite (`uv run pytest -m integration`) sends real messages through the
Bot API when `CHANNEL_TELEGRAM_BOT_TOKEN` / `CHANNEL_TELEGRAM_DEFAULT_RECIPIENT`
are set
in the ambient environment, and skips cleanly otherwise; it never calls
`setWebhook`.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
