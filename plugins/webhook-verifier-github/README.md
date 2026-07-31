# tai42-webhook-verifier-github

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The GitHub webhook-signature verifier plugin for the TAI ecosystem — a
per-provider `WebhookVerifier` that authenticates each inbound GitHub delivery
before the platform parses or dispatches its payload.

> A webhook door can bind a named verifier to a topic; this plugin supplies the
> `github` verifier.

GitHub signs every webhook delivery with an HMAC-SHA256 over the **exact raw
request body**, keyed by the shared secret, and sends it in the
`X-Hub-Signature-256` header as `"sha256=" + hexdigest`. `GitHubWebhookVerifier`
recomputes that HMAC over the raw bytes and compares it in constant time. It
returns `None` on success and raises `WebhookVerificationError` on any failure.

Its only tai-\* dependency is `tai42-contract` (the interface it registers
through); the HMAC work is pure standard library (`hmac` / `hashlib`). It
**never** imports the skeleton — the plugin is contract-facing.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A trigger
fires a tool from an inbound event; a webhook verifier is the door's bouncer —
the per-provider check that authenticates a delivery before any hook runs. This
package is the GitHub verifier. The ecosystem is open-ended: any package can
supply a verifier, so this repo is this verifier's own full doc home, and the
documentation site covers the platform-level story:

- Triggers & webhooks concept: https://tai42.ai/concepts/triggers-and-webhooks
- Build a webhook verifier (author guide): https://tai42.ai/guides/authors/webhook-verifier
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-webhook-verifier-github
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` beside this repo first — `[tool.uv.sources]` resolves it from
the sibling path.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/webhook-verifier-github
```

## How it loads — `lifecycle_modules`

The platform loads this plugin through the manifest's **`lifecycle_modules`**
field. That field is **import-only**: the host simply imports each listed module
to run its registration side effect. Importing `tai42_webhook_verifier_github`
calls `tai42_app.webhook_verifiers.register("github", GitHubWebhookVerifier())`,
binding the `github` verifier on the app handle.

```jsonc
{
  // Import-only modules: loaded purely for their registration side effect.
  // `lifecycle_modules` are imported WITHOUT going through the extension
  // registry's validation — only `extensions_modules` run that. So this entry
  // is a plain registration, not an extension.
  "lifecycle_modules": ["tai42_webhook_verifier_github"]
}
```

`lifecycle_modules` entries are imported as-is, with no validation step —
only `extensions_modules` go through the extension registry's validation. This
entry is therefore a plain registration, not an extension.

## Configuration — the secret never leaves the environment

A verifier is bound to a topic with `{verifier, config}`. This plugin's `config`
holds only the **name** of the environment variable that carries the secret —
never the secret itself:

```json
{ "verifier": "github", "config": { "secret_env": "GITHUB_WEBHOOK_SECRET" } }
```

At verify time the secret is read from `os.environ[config["secret_env"]]`. A
**missing** env var raises loudly (`KeyError`) and an **empty** one raises
`ValueError` — verification fails **CLOSED**, so a misconfigured secret never
becomes a silently-unauthenticated door.

> **Secret hygiene.** The secret lives only in an environment variable. Never
> commit it to a file, a fixture, a manifest, or a URL. The example secret below
> (`It's a Secret to Everybody`) is GitHub's own published example — it is a
> placeholder for docs and tests, never a real secret.

## End-to-end

1. **Create the GitHub webhook.** In your repo → *Settings* → *Webhooks* → *Add
   webhook*, set the *Payload URL* to your platform's public door, e.g.
   `https://<your-host>/universal_webhook/github-events`, *Content type*
   `application/json`, and a **Secret**. Store that same secret in the
   environment as `GITHUB_WEBHOOK_SECRET` on the platform.

2. **List `tai42_webhook_verifier_github` in the manifest** under
   `lifecycle_modules` (see above) so the `github` verifier is registered at
   boot.

3. **Bind the verifier to the topic.** Through the skeleton's authenticated
   route:

   ```bash
   curl -X PUT https://<your-host>/api/hooks/topics/github-events/verifier \
     -H "Authorization: Bearer $TAI_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"verifier": "github", "config": {"secret_env": "GITHUB_WEBHOOK_SECRET"}}'
   ```

4. **Register a hook** on the `github-events` topic that runs a demo tool (e.g. a
   tool that logs the event). Now push to the repo → GitHub signs and POSTs the
   delivery → the door verifies the `X-Hub-Signature-256` HMAC → the hook fires
   and the tool runs. A delivery with a missing, malformed, or wrong signature is
   rejected before any hook runs.

### Simulate a signed delivery locally

Compute the signature over the exact body with the same secret and POST it — this
is what a genuine GitHub delivery looks like on the wire:

```bash
# The secret lives only in the environment — never hard-coded here.
export GITHUB_WEBHOOK_SECRET="It's a Secret to Everybody"   # GitHub's example placeholder

BODY='{"zen":"Keep it logically awesome."}'
SIG="sha256=$(printf '%s' "$BODY" \
  | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" -r \
  | cut -d ' ' -f1)"

curl -X POST http://127.0.0.1:8000/universal_webhook/github-events \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -H "X-Hub-Signature-256: $SIG" \
  --data-raw "$BODY"
```

`printf '%s'` (not `echo`) sends the body with no trailing newline, so the bytes
signed match the bytes POSTed exactly — the HMAC is over the raw body.

## Verification rules

`verify(body, headers, config)` is an `async` method — the caller awaits it. It
raises `WebhookVerificationError` when:

- the `X-Hub-Signature-256` header is **missing** (lookup is case-insensitive),
- the value is not prefixed exactly `sha256=` (e.g. a `sha1=` prefix),
- the hex after the prefix is not exactly **64** characters (wrong/truncated),
- the recomputed digest **does not match** (the final compare uses
  `hmac.compare_digest` — constant-time).

A missing `secret_env` env var raises `KeyError` and an empty one raises
`ValueError` (fails closed), not `WebhookVerificationError`.

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
