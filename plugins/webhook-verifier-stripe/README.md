# tai42-webhook-verifier-stripe

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Stripe webhook-signature verifier plugin for the TAI ecosystem — a
per-provider `WebhookVerifier` that authenticates each inbound Stripe delivery
before the platform parses or dispatches its payload.

> A webhook door can bind a named verifier to a topic; this plugin supplies the
> `stripe` verifier.

Stripe signs every webhook delivery with an HMAC-SHA256 over a payload built from
the timestamp, a literal `.`, and the **exact raw request body**
(`str(t).encode() + b"." + body`), keyed by the endpoint's signing secret. It sends
the timestamp and the resulting digest(s) in the `Stripe-Signature` header as
`t=<unix ts>,v1=<hex hmac>`, with **more than one `v1=` during a secret
rotation**. `StripeWebhookVerifier` recomputes that HMAC over the raw bytes and
compares each well-formed `v1` in constant time; it also rejects a **stale**
timestamp (replay defense). It returns `None` on success and raises
`WebhookVerificationError` on any failure.

Its only tai-\* dependency is `tai42-contract` (the interface it registers
through); the HMAC work is pure standard library (`hmac` / `hashlib`, with `time`
for the freshness clock). It **never** imports the skeleton — the plugin is
contract-facing.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A trigger
fires a tool from an inbound event; a webhook verifier is the door's bouncer —
the per-provider check that authenticates a delivery before any hook runs. This
package is the Stripe verifier. The ecosystem is open-ended: any package can
supply a verifier, so this repo is this verifier's own full doc home, and the
documentation site covers the platform-level story:

- Triggers & webhooks concept: https://tai42.ai/concepts/triggers-and-webhooks
- Build a webhook verifier (author guide): https://tai42.ai/guides/authors/webhook-verifier
- Ecosystem catalog: https://tai42.ai/reference/catalog

The current release line tracks the **7.x contract** (`tai42-contract>=7,<8`).

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-webhook-verifier-stripe
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/webhook-verifier-stripe
```

## How it loads — `webhook_verifier_modules`

The platform loads this plugin through the manifest's **`webhook_verifier_modules`**
field — the `webhook-verifier` kind's own declared binding. The host imports each
listed module to run its registration side effect. Importing
`tai42_webhook_verifier_stripe` calls
`tai42_app.webhook_verifiers.register("stripe", StripeWebhookVerifier())`, binding
the `stripe` verifier on the app handle.

```jsonc
{
  // `webhook_verifier_modules` is the webhook-verifier kind's own manifest
  // field: the host imports each listed module purely for its registration
  // side effect, binding the named verifier on the app handle.
  "webhook_verifier_modules": ["tai42_webhook_verifier_stripe"]
}
```

`webhook_verifier_modules` is the `webhook-verifier` kind's own declared field:
each entry is imported for its registration side effect, so listing
`tai42_webhook_verifier_stripe` registers the `stripe` verifier at boot.

## Configuration — the secret never leaves the environment

A verifier is bound to a topic with `{verifier, config}`. This plugin's `config`
holds only the **name** of the environment variable that carries the signing
secret — never the secret itself — plus an optional replay tolerance:

```json
{ "verifier": "stripe", "config": { "secret_env": "STRIPE_WEBHOOK_SECRET" } }
```

`config` accepts an optional `"tolerance_seconds"` (a positive int, default
`300` — Stripe's documented replay window):

```json
{ "verifier": "stripe", "config": { "secret_env": "STRIPE_WEBHOOK_SECRET", "tolerance_seconds": 300 } }
```

At verify time the secret is read from `os.environ[config["secret_env"]]`. A
**missing** env var raises loudly (`KeyError`) and an **empty** one raises
`ValueError`; a non-positive or non-int `tolerance_seconds` also raises
`ValueError` — verification fails **CLOSED**, so a misconfigured door never
becomes a silently-unauthenticated one.

> **Secret hygiene.** The signing secret lives only in an environment variable.
> Never commit it to a file, a fixture, a manifest, or a URL. Stripe's endpoint
> signing secret is shown once in the Dashboard as a `whsec_…` value; store it
> in the environment, not in the repository.

## End-to-end

1. **Create the Stripe webhook endpoint.** In the Stripe Dashboard →
   *Developers* → *Webhooks* → *Add endpoint*, set the endpoint URL to your
   platform's public door, e.g.
   `https://<your-host>/universal_webhook/stripe-events`, and select the events
   to send. Stripe shows the endpoint's **Signing secret** (`whsec_…`) once —
   store it in the environment as `STRIPE_WEBHOOK_SECRET` on the platform.

2. **List `tai42_webhook_verifier_stripe` in the manifest** under
   `webhook_verifier_modules` (see above) so the `stripe` verifier is registered
   at boot.

3. **Bind the verifier to the topic.** Through the skeleton's authenticated
   route:

   ```bash
   curl -X PUT https://<your-host>/api/hooks/topics/stripe-events/verifier \
     -H "Authorization: Bearer $TAI_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"verifier": "stripe", "config": {"secret_env": "STRIPE_WEBHOOK_SECRET"}}'
   ```

4. **Register a hook** on the `stripe-events` topic that runs a demo tool (e.g. a
   tool that logs the event). Now trigger a Stripe event → Stripe signs and POSTs
   the delivery → the door verifies the `Stripe-Signature` HMAC and freshness →
   the hook fires and the tool runs. A delivery with a missing, malformed, stale,
   or wrong signature is rejected before any hook runs.

### Simulate a signed delivery locally

Compute the signature over `t.body` with the same secret and POST it — this is
what a genuine Stripe delivery looks like on the wire:

```bash
# The secret lives only in the environment — never hard-coded here.
export STRIPE_WEBHOOK_SECRET="whsec_example_placeholder"   # your endpoint's signing secret

BODY='{"id":"evt_1","type":"checkout.session.completed"}'
TS="$(date +%s)"
SIG="$(printf '%s' "$TS.$BODY" \
  | openssl dgst -sha256 -hmac "$STRIPE_WEBHOOK_SECRET" -r \
  | cut -d ' ' -f1)"

curl -X POST http://127.0.0.1:8000/universal_webhook/stripe-events \
  -H "Content-Type: application/json" \
  -H "Stripe-Signature: t=$TS,v1=$SIG" \
  --data-raw "$BODY"
```

`printf '%s'` (not `echo`) sends the body with no trailing newline, so the bytes
signed match the bytes POSTed exactly — the HMAC is over `timestamp + "." + raw
body`.

## Verification rules

`verify(body, headers, config)` is an `async` method — the caller awaits it. It
raises `WebhookVerificationError` when:

- the `Stripe-Signature` header is **missing** (lookup is case-insensitive),
- the header is **unparsable**: no `t=` element, a **duplicate** `t=`, a
  non-integer `t`, or no `v1=` element at all,
- the timestamp is **stale** (`now - t > tolerance_seconds`) — a future
  timestamp is accepted by design, so clock skew on the sender is not treated as
  an attack,
- **no well-formed `v1` candidate matches** the recomputed digest (the compare
  uses `hmac.compare_digest` — constant-time). A malformed `v1` (wrong length or
  non-hex) is **skipped**, not raised on, so one unusable signature during a
  secret rotation does not reject a delivery that also carries a good one; a
  header whose `v1` values are all malformed fails here as a plain no-match.

Any non-`v1` scheme key (`v0=`, anything unrecognized) is ignored. A missing
`secret_env` env var raises `KeyError`, and an empty secret or a non-positive /
non-int `tolerance_seconds` raises `ValueError` (fails closed), not
`WebhookVerificationError`.

**Replay defense.** After a delivery passes verification, the verifier claims its
Stripe `event.id` in a seen-set, so a captured signed delivery replayed inside the
tolerance window is refused rather than dispatched again. The seen-set TTL runs until
the signed freshness window ends (`t + tolerance_seconds`) — anchored to the signed
timestamp and rounded up so an id is never forgotten before that window ends.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
