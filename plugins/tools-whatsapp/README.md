# tai42-tools-whatsapp

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

WhatsApp provisioning tools for the TAI ecosystem — manifest-loaded Meta Graph
tools that register a message template, list the business account's message
templates, delete a template by name, and subscribe the app to a WhatsApp
Business Account's webhooks.

All Graph traffic is direct REST through tai42-kit's curl client (no vendor
SDK); the Graph host and pinned API version ride the settings below. The package
registers its tools through the `tai42_app` handle from `tai42_contract.app` and
never imports the skeleton.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A tool is a
callable the host loads from a plugin manifest; this package supplies the
WhatsApp provisioning tools. The ecosystem is open-ended, so this repo is these
tools' own full doc home, and the documentation site covers the platform-level
story:

- Tools concept: https://tai42.ai/concepts/tools
- Build a tool (author guide): https://tai42.ai/guides/authors/tool
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-tools-whatsapp
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/tools-whatsapp
```

## Catalog

| Tool | Description |
| --- | --- |
| `register_whatsapp_template` | Registers a message template on the account and returns Graph's id and review status. |
| `list_whatsapp_templates` | Lists every message template on the account, following Graph's paging cursors to exhaustion. |
| `delete_whatsapp_template` | Deletes a message template by name (percent-encoded into the query) and returns Graph's response. |
| `subscribe_whatsapp_app` | Subscribes the app to the account's webhooks, optionally overriding the callback URI and verify token. |

## Registering templates

`register_whatsapp_template` takes `name`, `language`, `category`, and a
Graph-shaped `components` list (all required and non-empty). `components` is
passed through unchanged — Graph validates its shape and rejects a malformed one
with its own error, which propagates. The tool returns Graph's response (the new
template's id and review status).

`list_whatsapp_templates` returns the concatenated `data` across every page,
following Graph's `paging.next` cursor until Graph stops sending one, so a long
template catalog is never silently truncated. `delete_whatsapp_template` takes a
single `name`, percent-encoded into the delete query.

## Subscribing webhooks

`subscribe_whatsapp_app` subscribes the app to the account's webhooks. With both
`callback_uri` and `verify_token` supplied it overrides the app's configured
webhook with Meta's `override_callback_uri` + `verify_token` pair; with neither
it subscribes to the app's configured webhook. Supplying exactly one of the two
raises.

## Configuration

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| Access token | `CHANNEL_WHATSAPP_ACCESS_TOKEN` | — | Graph API bearer token. Required. |
| Business account | `CHANNEL_WHATSAPP_WABA_ID` | — | WhatsApp Business Account id the endpoints hang under. Required. |
| API base | `CHANNEL_WHATSAPP_API_BASE_URL` | `https://graph.facebook.com/v23.0` | Graph API origin with a pinned version. |
| Request timeout | `CHANNEL_WHATSAPP_HTTP_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout. |

The token and WABA id share the `CHANNEL_WHATSAPP_` env group with the rest of
the WhatsApp deployment. Secrets live only in the environment; the access token
is held as a secret and never appears in a log or a raised error. A missing or
empty required credential raises loudly (fails closed), and every non-2xx from
Graph raises with Graph's status and body — never a silent default.

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
