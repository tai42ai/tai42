# Real-vendor setup

Selecting a service with `TAI_E2E_REAL=<service>` swaps its mock for the live
vendor. Most of that is env-only — the harness fills the credentials from the
filled `REAL_E2E_CREDENTIALS.env`. But some vendors need a dashboard
registration the harness cannot perform: an inbound webhook, a redirect URI, an
org hook. This is the operator checklist for those — one section per real leg
that has a manual step, plus the outbound-only legs that have none.

Read `REAL_E2E_CREDENTIALS.env.example` alongside this file: it is the source of
truth for every template var named below. The loop is always template → dashboard
→ run.

## What real-select does to the mock legs

Not every seam is asserted the same way when it goes real. Many seams have a leg
that reads outbound traffic off an in-process stub (scripted `llm_stub`,
`FakeStripe`, `FakeTwilio`, `FakeWhatsApp`, `FakeSlack`, `FakeTelegram`, the
`OAuthIdp` issuer, `FixturePackageIndex`, the fixture connector client_ids). Those
**mock legs step aside** under real-select via a `skipif(HarnessSettings().is_real(<seam>))`
guard (or, where a spec is parametrized across channels, a per-param skip), so
naming that seam real deselects the mock leg rather than running it against a live
vendor it was never written to reach. The real leg for these is demonstrated on
the dedicated e2e creds host, not in CI. The seams that step aside:

- `llm`, `embeddings` — scripted-stub determinism legs.
- `stripe` — the `FakeStripe` mint / webhook / reconcile legs.
- `twilio`, `whatsapp`, `slack`, `telegram` — the channel **round-trip / notify /
  allowlist-delivery** legs (they wait for a send off the stub). Their real-safe
  negatives, which assert `sends_matching(...) == []`, do NOT step aside — a real
  send simply never lands. Only `test_callback_origin.py` is a real-aware origin
  unit test; it is not a stub-delivery leg.
- `connector-google`, `connector-atlassian` — the launch-URL-shape legs that pin
  the fixture `client_id`.
- `oidc` — the `OAuthIdp`-stub login / issuer-JWT legs (`github-login` has **no
  mock leg to step aside**: it is a real-only additive OIDC provider).
- `k8s` — the fake-apiserver ConfigMap/Secret round-trip (`test_config_k8s_stack.py`)
  steps aside; its real leg (`test_config_k8s_real_branch.py`) proves the real-cluster
  wiring is READY without needing a live cluster in CI.
- `marketplace-pypi` — steps the **entire** marketplace suite aside (opt-in behind
  `TAI_E2E_MARKETPLACE=1`): the shared `marketplace_service` fixture seeds a forged
  fixture catalog through the real seed+ingest pipeline, which can't resolve those
  packages from real pypi.org, so every module would false-fail at setup.
- `marketplace-github` — steps aside **only** the github-ingest / webhook leg
  (`test_webhook_ingest.py`); the pypi override stays set, so the pypi-sourced
  browse siblings still run.

Because `build_bridge_stack` also wires the LLM env, `TAI_E2E_REAL=llm` steps the
bridge modules aside too, not only the channel seams. All guards are inert while
`TAI_E2E_REAL` is empty — the default mock suite collects and runs byte-for-byte
unchanged.

What still carries **real assertions at the test layer** under its own seam: the
`storage-s3` / `storage-github` round-trips are axis-driven — the *same* test runs
against the `*-real` backend (selected by `TAI_E2E_STORAGE`) rather than stepping
aside; and `langfuse` runs against the real Langfuse either way (self-hosted or
cloud) — no stub, no gate.

## 0. The public host — do this first

Real inbound webhooks arrive from the internet; loopback (as CI uses) cannot
receive them. Every dashboard URL below is relative to a single knob:

- `E2E_PUBLIC_BASE_URL` — a stable public **HTTPS** host with its own domain (a
  durable managed endpoint, not an ad-hoc tunnel), reverse-proxying to the e2e
  stack-under-test port. Real legs that need inbound refuse to start without it.
- The filled `REAL_E2E_CREDENTIALS.env` lives **only** on that dedicated e2e
  host. Real legs run there (via `workflow_dispatch` / scheduled job), never on
  PRs.

Endpoint-origin settings the harness derives from `E2E_PUBLIC_BASE_URL` at wiring
time — leave the `[AUTO-DERIVED]` entries alone: `CHANNEL_TELEGRAM_PUBLIC_BASE_URL`,
`TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL`, `INTERACTIONS_PUBLIC_BASE_URL`. Every URL
below is written `{E2E_PUBLIC_BASE_URL}/...`.

## Inbound-webhook vendors

Each of these needs a webhook registered in the vendor dashboard pointing at the
public host. No API automates the registration.

### slack — `TAI_E2E_REAL=slack`

- Dashboard: https://api.slack.com/apps → your app → **Event Subscriptions** →
  enable, Request URL = `{E2E_PUBLIC_BASE_URL}/api/channels/slack/inbound`.
- Template vars: `CHANNEL_SLACK_BOT_TOKEN` (xoxb-, `chat:write` scope,
  installed), `CHANNEL_SLACK_SIGNING_SECRET`, `CHANNEL_SLACK_TEST_CHANNEL_ID`
  (C…, a test channel the bot is invited to — becomes the recipient allowlist).
- Bridge route only: `CHANNEL_SLACK_BOT_USER_ID` (U…, the bot's own member id
  from App Home / `auth.test`) — the self-message filter and `our_identity`.
  Notify / ask_user / signature verification run without it.

### twilio — `TAI_E2E_REAL=twilio`

- Dashboard: https://console.twilio.com → your number → Messaging webhook =
  `{E2E_PUBLIC_BASE_URL}/api/channels/twilio/inbound` (one-time; the plan can
  also set it via the IncomingPhoneNumbers API).
- Template vars: `CHANNEL_TWILIO_ACCOUNT_SID`, `CHANNEL_TWILIO_AUTH_TOKEN`,
  `CHANNEL_TWILIO_FROM`, `CHANNEL_TWILIO_TEST_TO`. To run over WhatsApp instead of
  SMS there is no separate leg: set `CHANNEL_TWILIO_FROM` / `CHANNEL_TWILIO_TEST_TO`
  to `whatsapp:`-prefixed values (the plugin passes them through unchanged).

### whatsapp — `TAI_E2E_REAL=whatsapp`

- Dashboard: https://developers.facebook.com → your Meta app → WhatsApp →
  Configuration → webhook subscribe, Callback URL =
  `{E2E_PUBLIC_BASE_URL}/api/channels/whatsapp/inbound`.
- The SUT must be publicly reachable **during** the subscribe: Meta fires a GET
  verify handshake against that URL and the stack must answer it before the
  subscription saves.
- Template vars: `CHANNEL_WHATSAPP_ACCESS_TOKEN`, `CHANNEL_WHATSAPP_APP_SECRET`,
  `CHANNEL_WHATSAPP_VERIFY_TOKEN` (operator-chosen shared secret — the same value
  you register in Meta's subscription; read verbatim by the plugin, NOT
  harness-generated), `CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID`,
  `CHANNEL_WHATSAPP_TEST_TO`.

### stripe (webhook leg) — `TAI_E2E_REAL=stripe`

- Dashboard: https://dashboard.stripe.com (TEST mode) → Developers → Webhooks →
  Add endpoint = `{E2E_PUBLIC_BASE_URL}/universal_webhook/<topic>` (topic name
  free but must match the topic the verifier + hook are wired to, e.g.
  `payments`); subscribe event `checkout.session.completed`; copy the endpoint
  Signing secret.
- Template vars: `STRIPE_SECRET_KEY` (sk_test_), `STRIPE_WEBHOOK_SECRET` (whsec_,
  the value the harness binds the topic's `secret_env` to).
- Outbound-only stripe (create/list + reconciler) needs **no** webhook — see
  below.

## OAuth redirect URIs

These legs open a real browser consent flow, so each vendor must whitelist a
redirect URI for the public origin. The harness derives the exact redirect URI
from `E2E_PUBLIC_BASE_URL` at wiring time; register that origin at the vendor.

### connector-google — `TAI_E2E_REAL=connector-google`

- Dashboard: https://console.cloud.google.com → enable Gmail/Calendar/Drive
  APIs, OAuth consent screen (External, add the test account as test user),
  Credentials → OAuth client ID (Web application) with the derived redirect URI.
- Template vars: `CONNECTORS_GOOGLE_CLIENT_ID`, `CONNECTORS_GOOGLE_CLIENT_SECRET`,
  `CONNECTORS_GOOGLE_TEST_ACCOUNT_EMAIL`.

### connector-atlassian — `TAI_E2E_REAL=connector-atlassian`

- Dashboard: https://developer.atlassian.com/console/myapps → OAuth 2.0 (3LO)
  app, Jira + Confluence scopes, callback URL = the derived redirect URI.
- Template vars: `CONNECTORS_ATLASSIAN_CLIENT_ID`,
  `CONNECTORS_ATLASSIAN_CLIENT_SECRET`, `CONNECTORS_ATLASSIAN_SITE_URL`,
  `CONNECTORS_ATLASSIAN_TEST_ACCOUNT_EMAIL`.

### oidc (Auth0) — `TAI_E2E_REAL=oidc`

- Dashboard: https://manage.auth0.com → Applications → Regular Web Application,
  callback URL = the derived redirect URI; APIs → create API (its Identifier is
  the audience); one test user.
- Template vars: `AUTH0_ISSUER`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`,
  `AUTH0_AUDIENCE`, `AUTH0_TEST_USER_EMAIL`, `AUTH0_TEST_USER_PASSWORD`
  (harness-mapped → a `TAI_ACCOUNTS_OIDC_PROVIDERS` `preset:"auth0"` row +
  `TAI_IDENTITY_OIDC_ISSUER`/`_AUDIENCE`).

### github-login — `TAI_E2E_REAL=github-login`

- Dashboard: https://github.com/settings → Developer settings → OAuth Apps →
  New OAuth App, callback URL = the derived redirect URI.
- Template vars: `GITHUB_LOGIN_CLIENT_ID`, `GITHUB_LOGIN_CLIENT_SECRET`
  (harness-mapped → a `TAI_ACCOUNTS_OIDC_PROVIDERS` `preset:"github"` row).

## marketplace-github — `TAI_E2E_REAL=marketplace-github`

Only needed to exercise **live tag-push ingest**. Register a GitHub **org
webhook** pointing at the marketplace registry ingest endpoint so a real tag
push drives the registry.

- Template vars: `MP_GITHUB_TOKEN` (public-repo read, raises rate limits),
  `MP_GITHUB_WEBHOOK_SECRET` (AUTO).
- Without the live-ingest requirement, marketplace-github still runs outbound
  (real api.github.com reads) with no webhook.

## telegram — `TAI_E2E_REAL=telegram` — NO manual step

The plugin self-registers its webhook with `setWebhook` at boot from
`CHANNEL_TELEGRAM_PUBLIC_BASE_URL` (auto-derived). Nothing to register in any
dashboard. Template vars: `CHANNEL_TELEGRAM_BOT_TOKEN`,
`CHANNEL_TELEGRAM_WEBHOOK_SECRET` (AUTO), `CHANNEL_TELEGRAM_TEST_CHAT_ID`.

## Purely outbound — no public endpoint

These legs only make outbound calls to the vendor; they need credentials but no
dashboard registration and no inbound webhook. Set the vars and run.

| Service (`TAI_E2E_REAL=`) | Template vars |
|---|---|
| `llm` | `REAL_E2E_LLM_PROVIDER` (default openai), the selected provider's key (e.g. `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`), optional `REAL_E2E_LLM_MODEL` |
| `embeddings` | `REAL_E2E_EMBEDDING_PROVIDER` (default openai; openai/mistral/google/huggingface), that provider's key, optional `REAL_E2E_EMBEDDING_MODEL` (toggles independently of `llm`) |
| `langfuse` | `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` |
| `storage-s3` | `STORAGE_S3_BUCKET`, `_REGION`, `_ACCESS_KEY`, `_SECRET_KEY`, `_ENDPOINT`, `_ADDRESSING_STYLE`, `_REQUEST_CHECKSUM_CALCULATION` — **also set `TAI_E2E_STORAGE=s3-real`** |
| `storage-github` | `STORAGE_GITHUB_USERNAME`, `_REPO`, `_BRANCH`, `_TOKEN` — **also set `TAI_E2E_STORAGE=github-real`** |
| `k8s` | `KUBECONFIG`, `TAI_K8S_NAMESPACE` — before the run, seed that namespace with a ConfigMap `tai-manifest` (its `manifest.yml` key holds the stack manifest) and a Secret `tai-env`; the real leg reads/patches those exact names there, not the fake leg's `e2e` |
| `marketplace-pypi` | none — reads real pypi.org anonymously (toggles independently of `marketplace-github`) |
| `stripe` (outbound-only) | `STRIPE_SECRET_KEY` — create/list + reconciler recover without the webhook |

The two storage seams are **dual-knob**: `TAI_E2E_REAL=storage-s3` (or
`storage-github`) only enables the credential loud-fail; the storage axis
`TAI_E2E_STORAGE=s3-real` (or `github-real`) is what actually boots the real
backend. Naming the seam on `TAI_E2E_REAL` without setting the matching axis fails
loudly at collection (never a silent fall-back to the hermetic mock).

Notify-only channel sends (outbound `chat.postMessage` and friends) also need no
public endpoint — only the inbound legs above do.
