# tai42-skeleton

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The open-source implementation of `tai42-contract` — the extended MCP server that
hosts tools, agents, extensions, connectors, hooks, and storage for the TAI
ecosystem. It provides the concrete `TaiMCP` server and the runtime engines
(tool registry and adapters, the agent registry, the OAuth connector engine, the
access-control middleware, the hooks router, the template/storage manager, the
manifest loader, and the transport layer) that implement the protocols declared
in `tai42-contract`.

Providers — OAuth connectors, storage backends, config providers, worker
backends, monitoring — ship as separate plugins that register through the
`tai42_app` contract handle when the manifest loads them; no plugin imports the
skeleton.

## Position in the ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows — the server
that hosts a capability and supplies the operational layer around it (manifest
loading, access control, OAuth connectors, background execution, monitoring,
storage, and human-in-the-loop steps).

Three packages; each depends only on the ones to its left:

```
tai42-contract  <--  tai42-kit  <--  tai42-skeleton
(interfaces)      (helpers)     (the server)
```

`tai42-skeleton` is the server at the end of the chain: it depends on **only**
`tai42-contract` (the pure interface package) and `tai42-kit` (generic leaf
helpers). It is the runnable body every plugin plugs into.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-skeleton
```

Or from source:

```bash
git clone https://github.com/tai42ai/tai42
cd core/skeleton
uv venv --python 3.13
uv pip install --no-sources --editable .
```

Add the `toolbox` extra for batteries — it pulls in the `tai42-toolbox` contrib
package, whose composition tool extensions (`chain`, `batch`) and generic tool
collection load from the manifest (see [`examples/toolbox`](examples/toolbox)):

```bash
uv add "tai42-skeleton[toolbox]"                      # from PyPI
uv pip install --no-sources --editable ".[toolbox]"   # from a source checkout
```

## Run it

The hello-world app in [`examples/hello`](examples/hello) registers one local
`greet` tool and needs no services to stand up. From the repo root:

```bash
ACCESS_CONTROL_ENABLE=false \
PYTHONPATH=examples/hello \
uv run --no-sync tai serve --manifest-path examples/hello/manifest.yml --port 8765
```

The MCP endpoint is then `http://127.0.0.1:8765/mcp`, and it lists 94 tools, not
one: alongside `greet` the server projects 93 **operation tools** from its own
management operations, because the manifest's `api_tools` block is on by
default. Set `api_tools: {enabled: false}` to switch that projection off and
leave `greet` on its own. The server also polls the marketplace for security
advisories on a timer — that is its one outbound call, and
`MARKETPLACE_ADVISORIES_POLL=false` turns it off.

`tai backend` runs the agent/worker backend process and `tai metrics` serves the
Prometheus endpoint; the full command surface is in the CLI reference below.

`tai serve`, `tai backend`, and `tai metrics` form one run family over a single
shared Prometheus multiprocess directory (`PROMETHEUS_MULTIPROC_DIR`, default a
fixed absolute path under the host temp dir; any override must be absolute).
Point all three at the same directory and restart them together: `tai serve`
clears the directory once at boot, so restarting it mid-run while a backend
worker is still writing orphans that worker's counters until the whole family
restarts.
[`examples/README.md`](examples/README.md) walks through the examples, and
[`examples/manifest.yml`](examples/manifest.yml) is the commented manifest
reference.

## The worker bus

When a deployment runs more than one process — several `tai serve` workers, a
`tai backend` runtime alongside the server, or multiple pods sharing one config —
a manifest edit on one process must reach the others, or siblings serve stale
state. The **worker bus** is how every process converges: each subscribes to one
Redis channel at startup, a mutation applies locally and is then broadcast, and
the response carries a per-origin report of how every worker fared.

The bus is **internal app infrastructure, like the reload gate — it is NOT a
plugin.** Nothing about it is registrable, swappable, or user-selectable; there is
exactly one bus and no manifest field chooses an implementation. It is configured
only by environment: set `TAI_BUS_REDIS_URL` (plus the optional `TAI_BUS_*` knobs)
to turn it on. A single-worker, file-mode, no-backend deployment needs no bus and
runs on a no-op local variant; a multi-worker, backend-bearing, or `k8s`-mode boot
refuses to start without one, naming `TAI_BUS_REDIS_URL`. On a shared Redis,
`TAI_BUS_NAMESPACE` must diverge per stack — Redis pub/sub is server-global, so
co-tenant deployments would otherwise cross-talk.

## Conversations and person linking

The conversation bridge turns one inbound message — from an authed API caller
(`door=api`) or a registered channel adapter (`door=channel`) — into an agent or
tool turn whose answer is durably stored and delivered back. A **route** binds a
door to a target: an `agent` run (threaded conversation memory) or a `tool`
dispatch (stateless, one call per message). Routes are managed over
`POST /api/conversations/{route_name}` (the `tai conversations create` CLI), and a
thread reads over `/api/conversations/{route_name}/threads` and `/transcript`. An
unlinked conversation keys its thread `bridge:{route_name}:{address}`.

One person who reaches a target on several channels can prove, by an explicit pair
code, that those addresses are the same person — and the bridge then treats them
as **one person for that target**. Nothing changes for anyone who never pairs: an
unlinked conversation keeps its route-keyed thread byte-for-byte. Person linking is
**off by default** and opted in per target.

### Per-target conversation config

Each `(target_kind, target_name)` may carry a `TargetConversationConfig`:

| Field | Meaning |
|---|---|
| `multichannel` | `bool`, default `false`. Opts the target into person linking. With it off, `/link`, `/unlink` and any pair code reach the target as ordinary text — no behavior change. |
| `greeting_template` | `str \| None`, default `None`. A first-contact greeting. `None` means no greeting; a blank string is refused — `null` is the spelling for "no greeting". |

`greeting_template` may reference **only** the `{pairing_code}` placeholder; any
other `{...}` field is refused at write time so a typo cannot render literally.
When the template references `{pairing_code}`, a fresh single-use code is minted at
greeting time and substituted in. The greeting fires on **first contact only** —
the first message the bridge admits from an address on that target; a later message
from the same address carries no greeting. It also fires **only on a
`multichannel: true` target** — the greeting lives inside the multichannel path, so
a greeting set on a target with multichannel off stays inert until multichannel is
enabled (the two fields are accepted independently).

Set it over the config API / CLI (`config-set` is an upsert and requires the agent
or tool target to exist):

```bash
# PUT /api/conversation-configs/{target_kind}/{target_name}
tai conversations config-set agent concierge --multichannel \
  --greeting-template 'Hi! Pair another channel with {pairing_code}'
tai conversations config-list                  # GET /api/conversation-configs
tai conversations config-get agent concierge   # GET .../{target_kind}/{target_name}
tai conversations config-delete agent concierge # DELETE .../{target_kind}/{target_name}
```

### Pairing protocol

On a target with `multichannel: true`, the bridge intercepts three exact things in
the inbound text — everything else passes through unchanged to the target:

- `/link` (the whole trimmed message) — mints a fresh pair code and replies with
  it. `/link extra` is ordinary text, not a command.
- `/unlink` (the whole trimmed message) — detaches the **sending** address from its
  person; the rest of the person stays linked, and the detached address returns to
  its own route-keyed thread.
- a **pair code** anywhere in the text — `LINK-` followed by 8 `[A-Z0-9]`
  characters (matched as `\bLINK-[A-Z0-9]{8}\b`, so a code pasted inside a sentence
  still redeems). Redeeming merges the two conversations into one person.

A pair code is **single-use** and expires **15 minutes** after it is minted. One
code is open per conversation at a time: minting again **rotates** — a fresh code
is issued and the previous one stops working (the newest code wins). The raw code
exists only in flight and is never stored recoverably.

Merging is **transitive**: each redemption folds the two persons into one (the
union of their addresses), so a chain of pairings across three or more channels
always converges to a single person for that target.

Both doors join persons. On the `channel` door the address is the medium's (a phone
number, a chat id, ...); on the `api` door it is the composed caller/end-user
string. An API caller whose address belongs to a linked person reads the full
merged person thread. Errors are uniform: an unknown, expired, or already-redeemed
code all get the same refusal reply, so the wording reveals nothing about whether a
code ever existed.

Person **scope is per target**: pairing on one `(target_kind, target_name)` never
links addresses on any other target.

**Known limits**

- **Histories are never merged.** Linking two conversations does not stitch their
  two past transcripts together; the shared person thread starts at the pairing
  moment (agent targets — a tool target has no thread and shares identity only).
- **Slack pairs the room.** A Slack channel address is the room, not a user, so
  pairing on Slack links the room rather than one person in it.
- **Web identity is a browser cookie.** A web visitor who clears cookies or opens
  another browser/device is a new person until they pair again; an invite link
  re-pairs them in one load (see the `channel-web` plugin's invite links).

### The `get_pairing_code` tool

`get_pairing_code` is a runtime-native builtin that mints a pair code for a live
channel conversation, so an operator can compose an invite (a typed code, or a
`channel-web` `?pair=` URL) around it. Opt the module in with a manifest
`tools[].module` row:

```yaml
tools:
  - title: get-pairing-code
    module: tai42_skeleton.tools.builtin.get_pairing_code
```

| Parameter | Meaning |
|---|---|
| `channel` | The registry channel name the conversation runs on (e.g. `telegram`). |
| `our_identity` | The medium address the conversation is texted at — the route's identity for `channel`. |
| `sender` | The address the code will link. |

It returns `{"code", "expires_at"}` and **nothing else** — no link and no wording;
the operator composes the invite text themselves. Calling it again rotates the
conversation's open code (newest wins). Refusals are loud: a blank `channel`,
`our_identity`, or `sender` raises; a null/absent argument — which the api door
carries, having no channel identity — is refused by the typed signature before the
tool body runs; and a resolved target with `multichannel` off is refused with no
code minted.

**Wiring it as a tool-target route.** A `tool` route maps the inbound message to
the tool's kwargs with a `payload_expr` (jq) and the result to the reply with a
`reply_expr` (jq). The payload the expr runs over is
`{message, sender, our_identity, channel}` — `our_identity` and `channel` are the
route's, `sender` is the conversation address. Map those onto the tool's own
parameter names and pull `.code` out of the result:

```bash
tai conversations create pair-mint --door channel --target-kind tool \
  --target-name get_pairing_code --execution-key svc \
  --channel telegram --identity <bot-id> \
  --payload-expr '{channel: .channel, our_identity: .our_identity, sender: .sender}' \
  --reply-expr '.code'
```

`payload_expr` must emit exactly one JSON object; `reply_expr` must emit null or a
string — here the raw code — so the route replies with just the code. The tool
mints for the conversation named by `(channel, our_identity)`, whose resolved
target must itself have `multichannel` on.

## Development

Set up the dev venv and run the gates. `--no-sources` ignores the workspace-source
overrides in `[tool.uv.sources]`, so the dev deps come from PyPI and the clone stands
alone; `--no-sync` runs each gate against that environment instead of re-resolving:

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

See `CONTRIBUTING.md` for the rules.

## Documentation

The whole platform — the quickstart, concepts, guides, and the generated
reference — lives in the unified documentation site:

- Getting started & install: https://tai42.ai/getting-started/installation
- Quickstart: https://tai42.ai/getting-started/quickstart
- Concepts: https://tai42.ai/concepts
- Guides: https://tai42.ai/guides
- Built-in tools & extensions: https://tai42.ai/concepts/tools-and-extensions
- Reference (HTTP API, CLI, Python SDK): https://tai42.ai/reference/cli

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
