# tai42-tools-stripe

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Stripe payment tools for the TAI ecosystem — manifest-loaded tools that mint a
hosted payment link, answer a paid ask from a Stripe webhook, reconcile payments
the webhook path lost, mint a flexible-amount non-blocking payment link, expire
an issued checkout session early, and provision Stripe webhook endpoints (create,
list, delete).

All Stripe traffic is direct REST through tai42-kit's curl client (no `stripe`
SDK); the `Stripe-Version` header is pinned in code. The package registers its
tools through the `tai42_app` handle from `tai42_contract.app` and never imports
the skeleton.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A tool is a
callable the host loads from a plugin manifest; this package supplies the Stripe
payment tools. The ecosystem is open-ended, so this repo is these tools' own full
doc home, and the documentation site covers the platform-level story:

- Tools concept: https://tai42.ai/concepts/tools
- Build a tool (author guide): https://tai42.ai/guides/authors/tool
- Ecosystem catalog: https://tai42.ai/reference/catalog

The current release line tracks the **7.x contract** (`tai42-contract>=7,<8`).

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-tools-stripe
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/tools-stripe
```

## Catalog

| Tool | Description |
| --- | --- |
| `create_stripe_checkout` | Creates a Stripe Checkout Session and returns its hosted payment URL, usable directly as the `link` builder for an external ask. |
| `confirm_stripe_payment` | Webhook bridge: answers a paid checkout ask from a projected Stripe event. Not a user/agent tool — it holds the bridge secret. |
| `reconcile_stripe_payments` | Recovery layer: re-answers paid sessions the webhook path lost, re-derived from Stripe's own session list. Not a user/agent tool. |
| `create_stripe_payment_link` | Flexible-amount, non-blocking: mints a hosted payment link with no callback and returns its URL and session id. Deployment-fenced to deterministic flow callers. |
| `expire_stripe_checkout` | Expires an `open` Checkout Session early and returns its id and post-expire status; a non-open (completed/expired) session raises Stripe's 400. |
| `create_stripe_webhook_endpoint` | Provisions a Stripe webhook endpoint and returns its id and one-time signing secret (returned to the caller, never persisted here). |
| `list_stripe_webhook_endpoints` | Lists the account's webhook endpoints (id, url, status, events); Stripe never returns signing secrets post-creation. |
| `delete_stripe_webhook_endpoint` | Deletes a webhook endpoint by id and returns Stripe's deletion confirmation. |

## The payment ask (checkout / confirm / reconcile)

The first three tools implement a payment ask: an author asks a customer to pay, the customer
pays on a Stripe-hosted page, and a webhook answers the ask.

**Wiring the ask.** `create_stripe_checkout` is never called directly — it is the payment-link
builder behind the `ask_external` extension. Attaching `ask_external` to it in the manifest binds a
per-question `shared_secret` verifier (which authenticates the bridge to the callback door and turns
a browser GET into a hard 404) and composes a single tool, `create_stripe_checkout_ask_external`.
The verifier rides the attachment's author-bound `config`, so an agent can never supply or change it:

```yaml
tools:
  - title: stripe-checkout
    module: tai42_tools_stripe.tools.create_stripe_checkout
    extensions:
      create_stripe_checkout:
        - - name: ask_external
            config:
              verifier:
                name: shared_secret
                config:
                  header: X-TAI-Bridge-Secret
                  secret_env: TAI_BRIDGE_CALLBACK_SECRET
  - title: stripe-confirm
    module: tai42_tools_stripe.tools.confirm_stripe_payment
  - title: stripe-reconcile
    module: tai42_tools_stripe.tools.reconcile_stripe_payments
```

On the composed tool the builder's own parameters — `amount`, `currency`, `product_name`,
`success_url`, `cancel_url` — plus `answer_schema`, `question` and `timeout` are all call-time
arguments. The `answer_schema` const-pins the expected payment and is the amount binding: the door
validates the delivered answer against the stored schema and a mismatch 400s without consuming the
ticket. Its consts are typed on purpose — `amount_total` is an **integer** and `currency` a
**lowercase** string, because Stripe returns lowercase and the query-param answer path would
otherwise deliver strings. Every one of those money and payer-facing arguments is LLM-facing on the
composed tool, so they are pinned by a preset (next) and agents are given only the preset.

**Pin the money with a preset — agents get only the preset.** Under the `ask_external` composition,
`amount`, `currency`, `product_name`, `success_url`, `cancel_url` and the answer schema are all
LLM-facing arguments, so a prompt injection could charge any amount, redirect the payer, or drop
the pin. Bake all six as fixed constants a caller can neither supply nor override, with the schema's
`amount_total` const written from the **same literal** as `amount` so price and pin cannot drift:

```json
{
  "name": "buy_sample_item",
  "base_tool": "create_stripe_checkout_ask_external",
  "description": "Ask the payer to pay for a sample item.",
  "fixed_kwargs": {
    "amount": 50000,
    "currency": "usd",
    "product_name": "Sample item",
    "success_url": "https://acme.example/thanks",
    "cancel_url": "https://acme.example/cancelled",
    "answer_schema": {
      "type": "object",
      "required": ["amount_total", "currency"],
      "properties": {"amount_total": {"const": 50000},
                     "currency": {"const": "usd"}}
    }
  }
}
```

The preset's exposed schema is then `question` and `timeout` and nothing else. Do not add an
`extensions` key: an explicit empty `"extensions": []` is rejected — omit the field.

**Exposure.** `confirm_stripe_payment`, `reconcile_stripe_payments` and the raw
`create_stripe_checkout_ask_external` composed tool must **never** be exposed in `user_tools` or any
agent toolset — they hold the bridge secret and answer payment asks. Agents get the money-pinned
preset over the composed tool, never the tool itself.

**Livemode.** Both bridge tools assert a session's `livemode` against the configured
`STRIPE_SECRET_KEY`'s mode (`sk_live_`/`rk_live_` → live, `sk_test_`/`rk_test_` → test) and refuse
a mismatch loudly. Never point a test-mode webhook endpoint at a production payments topic: a
test-mode session is free to mint for any amount and satisfies a const pin exactly as a live one.

**Recovery is the reconciliation schedule.** A single webhook delivery is not a fulfillment
guarantee (the ingress ACKs before the hook runs), so `reconcile_stripe_payments` re-derives paid
sessions from Stripe's own list and re-answers anything the hook lost. Its default 26-hour
`lookback_hours` covers a Checkout link's full ~24h lifetime plus slack, and answers are paced by
`STRIPE_RECONCILE_ANSWER_INTERVAL_SECONDS` (default 1.2s) to stay off the callback door's rate
limiter. **Cadence and lookback are a coupled pair — state and tune them together**, never one
number alone: at a 15-minute cadence a 26-hour lookback re-scans each paid session on ~104
consecutive runs (every re-answer is an idempotent `already_answered` 200, but it is standing
load). After an outage LONGER than the lookback, run `reconcile_stripe_payments` once by hand with a
`lookback_hours` covering the outage — that is what the 168-hour ceiling exists to allow.

**Operator prerequisites.** Register the account webhook endpoint at the same API version the tools
pin (the endpoint's own API version governs the event payload shape); register the topic hook with
the canonical projection `expr` and a `checkout.session.completed` `condition`; set
`STRIPE_SECRET_KEY`, `TAI_BRIDGE_CALLBACK_SECRET` and `INTERACTIONS_PUBLIC_BASE_URL`; and schedule
`reconcile_stripe_payments`. `STRIPE_API_BASE` is settings-overridable and is a privileged
capability — a host it points at receives the `Authorization: Bearer <key>` header, so treat env
write as privileged.

## The non-blocking payment link (`create_stripe_payment_link`)

`create_stripe_payment_link` mints a flexible-amount Checkout Session through the same client seam
and `Stripe-Version` pin, but carries **no callback** and no ask machinery — it returns the hosted
payment URL and session id and does not wait for or answer anything:

```json
{ "link": "https://checkout.stripe.com/c/pay/cs_...", "session_id": "cs_..." }
```

It takes `amount` (minor units, ≥ 1), `currency` (3-letter lowercase), `product_name`,
`success_url`, `cancel_url`, and an optional `metadata` dict of caller key/value pairs. The session
is stamped with `tai_amount` and `tai_currency`, plus every caller `metadata` entry verbatim
alongside them. Caller values must be strings, no key may be empty, and no key may start with
`tai_` (reserved for the tool's own stamps). The created session's `livemode` is asserted against
the configured key's mode. Its `Idempotency-Key` is a canonical hash of every argument (with the
`metadata` dict sorted), so two calls with identical arguments return the **same** session —
distinct payments must differ in at least one argument, and a distinct `metadata` entry (e.g.
`external_ref` or `channel_user`) is the natural discriminator.

**Exposure.** This tool is **deployment-fenced to deterministic flow callers only** — its
money-facing arguments are all caller-supplied, so it must never be exposed on any agent or user
toolset.

## Expiring a checkout session (`expire_stripe_checkout`)

`expire_stripe_checkout` ends an issued Checkout Session before Stripe's ~24h default timeout,
through the same client seam and `Stripe-Version` pin. It takes a single `session_id` (required,
non-empty) and returns the session id and its post-expire status:

```json
{ "session_id": "cs_...", "status": "expired" }
```

Stripe expires only a session whose `status` is `open`; a completed or already-expired session is
a Stripe 400 that propagates unchanged — it is never remapped to success. The returned session's
`livemode` is asserted against the configured key's mode and a mismatch raises.

## Provisioning webhook endpoints (create / list / delete)

Three tools manage the account's Stripe webhook endpoints through the same client seam and
`Stripe-Version` pin.

`create_stripe_webhook_endpoint` takes an `https` `url` and a non-empty `enabled_events` list and
returns the endpoint and its **one-time signing secret**:

```json
{ "endpoint_id": "we_...", "secret": "whsec_...", "url": "https://…", "enabled_events": ["checkout.session.completed"], "status": "enabled" }
```

**The signing secret is revealed by Stripe only at creation.** This tool RETURNS it and writes it
nowhere — no config, no env, no file. Persisting it (through the platform's config API) is the
caller's job; a lost secret cannot be re-fetched. The `url` is where Stripe delivers events *to*
us, so it is validated as an `https` URL with a host and is **not** run through the outbound
callback SSRF pin (that pin guards URLs this process dials; this one is dialed by Stripe). The
created endpoint's `livemode` is asserted against the key's mode.

`list_stripe_webhook_endpoints` returns `{ "endpoints": [{ "endpoint_id", "url", "status",
"enabled_events" }] }` — Stripe never returns a signing secret post-creation.

`delete_stripe_webhook_endpoint` deletes an endpoint by id and returns `{ "endpoint_id", "deleted" }`.

**Rotating a signing secret.** Stripe has **no** rotate-secret endpoint. Rotation is: create a new
endpoint, swap the stored secret to the new one, then delete the old endpoint — these three tools
together enable it.

## Configuration

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| Secret key | `STRIPE_SECRET_KEY` | — | The Stripe secret/restricted key (`sk_…`/`rk_…`). Required. |
| API base | `STRIPE_API_BASE` | `https://api.stripe.com` | Stripe REST base. Privileged — receives the `Authorization` header. |
| Request timeout | `STRIPE_REQUEST_TIMEOUT_SECONDS` | `20` | Per-request timeout. |
| Reconcile pacing | `STRIPE_RECONCILE_ANSWER_INTERVAL_SECONDS` | `1.2` | Delay between reconcile answers. |
| Bridge secret | `TAI_BRIDGE_CALLBACK_SECRET` | — | Authenticates the bridge to the callback door. Required for confirm/reconcile. |
| Public base | `INTERACTIONS_PUBLIC_BASE_URL` | — | The origin the callback POST is pinned to. Required for confirm/reconcile. |

Secrets live only in the environment. A missing or empty required secret raises loudly (fails
closed) — never a silently-unauthenticated request.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --extra dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
