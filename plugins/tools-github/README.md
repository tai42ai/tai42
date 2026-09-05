# tai42-tools-github

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

GitHub provisioning tools for the TAI ecosystem — manifest-loaded repository-webhook
setup tools that create a repository webhook, list a repository's webhooks (following
`Link`-header pagination to exhaustion), and delete a repository webhook. They are the
provider-side setup counterpart to a runtime webhook-signature verifier: these tools
register the hook a verifier later authenticates.

All GitHub traffic is direct REST v3 through tai42-kit's curl client (no SDK); the
`X-GitHub-Api-Version` header is pinned in code. The package registers its tools through
the `tai42_app` handle from `tai42_contract.app` and never imports the skeleton.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A tool is a
callable the host loads from a plugin manifest; this package supplies the GitHub
repository-webhook provisioning tools. The ecosystem is open-ended, so this repo is these
tools' own full doc home, and the documentation site covers the platform-level story:

- Tools concept: https://tai42.ai/concepts/tools
- Build a tool (author guide): https://tai42.ai/guides/authors/tool
- Ecosystem catalog: https://tai42.ai/reference/catalog

The current release line tracks the **7.x contract** (`tai42-contract>=7,<8`).

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-tools-github
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/tools-github
```

## Catalog

| Tool | Description |
| --- | --- |
| `create_github_webhook` | Creates a repository webhook on `owner/name` and returns its id, delivery url, events and active flag. The caller supplies the signing secret; GitHub never returns it. |
| `list_github_webhooks` | Lists every webhook on `owner/name`, following the `Link` header to exhaustion, returning each hook's id, delivery url, events and active flag. |
| `delete_github_webhook` | Deletes the webhook `hook_id` from `owner/name`; succeeds only on GitHub's `204`, otherwise raises. |

## The signing secret is the caller's

GitHub does **not** mint the webhook secret — `create_github_webhook` sends the secret the
caller supplies, GitHub keys each delivery's `X-Hub-Signature-256` with it, and the caller
keeps its own copy for the verifier that authenticates inbound deliveries. GitHub never
returns the secret on create or list, and these tools never echo it: it appears in no
return value and in no raised message.

## Error model

Every non-2xx GitHub response raises loudly with the status and body — `create` and `list`
require a 2xx, `delete` requires exactly `204`. Nothing is caught or remapped to success,
and no raised message ever contains the token or the caller's secret. Redirects are not
followed: a 3xx off the pinned host would replay the `Authorization` header to another
origin, so it surfaces as its own status instead.

## Configuration

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| Token | `TOOLS_GITHUB_TOKEN` | — | The GitHub token (`Authorization: Bearer`). Required. |
| API base | `TOOLS_GITHUB_API_BASE` | `https://api.github.com` | GitHub REST base. Privileged — receives the `Authorization` header. |
| Request timeout | `TOOLS_GITHUB_REQUEST_TIMEOUT_SECONDS` | `20` | Per-request timeout. |

The token needs the scope GitHub requires to administer repository webhooks
(`admin:repo_hook` on a classic token, or repository `Webhooks` read/write on a
fine-grained token). Secrets live only in the environment. A missing or empty token raises
loudly (fails closed) — never a silently-unauthenticated request.

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
