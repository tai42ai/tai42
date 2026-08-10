# tai42-tools-twilio

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Twilio provisioning tools for the TAI ecosystem — manifest-loaded REST tools that
list a Twilio account's incoming phone numbers, read a number's inbound-message
webhook, and set a number's inbound-message webhook URL.

All Twilio traffic is direct REST through tai42-kit's curl client (no `twilio`
SDK); requests carry HTTP Basic auth (`AccountSid:AuthToken`). The package
registers its tools through the `tai42_app` handle from `tai42_contract.app` and
never imports the skeleton.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A tool is a
callable the host loads from a plugin manifest; this package supplies the Twilio
number-provisioning tools. The ecosystem is open-ended, so this repo is these
tools' own full doc home, and the documentation site covers the platform-level
story:

- Tools concept: https://tai42.ai/concepts/tools
- Build a tool (author guide): https://tai42.ai/guides/authors/tool
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-tools-twilio
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/tools-twilio
```

## Catalog

| Tool | Description |
| --- | --- |
| `list_twilio_numbers` | Lists the account's incoming phone numbers, following Twilio's paging to exhaustion, and returns each number's `sid`, `phone_number`, `sms_url`, and `sms_method`. |
| `get_twilio_number_webhook` | Reads one number's inbound-message webhook and returns its `sid`, `phone_number`, `sms_url`, and `sms_method`. |
| `set_twilio_number_webhook` | Sets a number's inbound-message webhook to an https URL and returns the updated `sid`, `phone_number`, and `sms_url`. |

## Provisioning a number's inbound webhook

These tools configure the Twilio side of an inbound-message flow: they point a
number's "A message comes in" webhook at the platform's inbound endpoint so
Twilio POSTs each message there. `list_twilio_numbers` finds the number's sid,
`get_twilio_number_webhook` reads its current webhook, and
`set_twilio_number_webhook` writes a new one:

```json
{ "sid": "PN...", "phone_number": "+15551234567", "sms_url": "https://app.example/api/channels/twilio/inbound" }
```

`set_twilio_number_webhook` requires an `https` URL and rejects an empty or
non-https value before any network call. Every Twilio non-2xx raises loudly with
its status and body — never remapped to success — and the Basic-auth header is
never echoed into the error.

## Configuration

The tools read the same `CHANNEL_TWILIO_` credentials as the deployment's Twilio
channel — one account SID, auth token, and REST base configure both.

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| Account SID | `CHANNEL_TWILIO_ACCOUNT_SID` | — | The Twilio account SID. Required. |
| Auth token | `CHANNEL_TWILIO_AUTH_TOKEN` | — | The Twilio auth token. Required. |
| API base | `CHANNEL_TWILIO_API_BASE_URL` | `https://api.twilio.com/2010-04-01` | Twilio REST origin (carries the API version prefix). |
| Request timeout | `CHANNEL_TWILIO_HTTP_TIMEOUT_SECONDS` | `30` | Per-request timeout. |

Credentials live only in the environment. A missing or empty required credential
raises loudly (fails closed), naming its env var and never the value.

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
