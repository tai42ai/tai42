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

Four packages; each depends only on the ones to its left:

```
                     tai42-kit  <--.
                   /  (helpers)     \
tai42-contract  <-+                  +--  tai42-skeleton
  (interfaces)     \                /    (the server)
                     tai42-cli  <--'
               (the remote client)
```

`tai42-skeleton` is the server at the end of the chain: it depends on
`tai42-contract` (the pure interface package), `tai42-kit` (generic leaf
helpers), and `tai42-cli` (the terminal client whose `tai` command it extends).
It is the runnable body every plugin plugs into.

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

A backend runtime may declare a process prelude: a `module:callable` that must run
before anything else is imported, as a green worker pool's patch must. `tai backend`
then reads its configuration, restarts itself once, in place, through that prelude
(same process id, same arguments, one `restarting with process prelude
<module:callable>` log line), and runs on the standard library's event loop with
asyncio's future chaining bound to the threads the prelude reshaped, so a future
another of those threads completes still wakes the loop.

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

## Platform event topics (9.0)

Since 9.0 the interaction failure events fire on hook-subscribable,
single-segment topics: `interactions_answer_rejected`,
`interactions_delivery_failed`, `interactions_ask_expired_unanswered`,
`interactions_callback_discarded`. The 8.x dotted spellings were
unregisterable by the hook door's topic charset, so no subscriber can
exist to break — but the constants' VALUES changed, which is a breaking
surface change and ships as this major.

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
runs on a no-op local variant; a multi-worker, backend-bearing, or non-file
config-mode boot refuses to start without one, naming `TAI_BUS_REDIS_URL`. On a shared Redis,
`TAI_BUS_NAMESPACE` must diverge per stack — Redis pub/sub is server-global, so
co-tenant deployments would otherwise cross-talk. Every bus key is under the namespace
and the running skeleton's major version, so workers of two majors on one Redis are two
separate fleets: a rolling replacement across majors runs both side by side until the
older workers are gone.

The bus does not carry sub-MCP app registrations: those live in their own store.
Under `tai serve` with more than one worker, each worker refuses to boot while that
store is in-memory, naming `SUB_MCP_REDIS_URL` (or the shared `TAI_DEFAULT_REDIS_URL`
it falls back to), because a sub-MCP app registered through one worker would not be
served by its siblings. It refuses an in-memory hooks registry the same way, naming
`HOOKS_REDIS_URL` (or `TAI_DEFAULT_REDIS_URL`): a hook registered through one worker
would not run for the events its siblings accept. Under `tai serve` with more than
one worker, each worker also refuses to boot without the bus. The workers check these
rules after the stored env is applied, so a URL kept only in the config-dir `.env`
or an external provider counts, and a config reload or config write that would leave
a multi-worker server without the bus or with either store in-memory is refused too. A config that registers a task
backend needs that shared registry too, whatever the worker count: `tai serve`,
`tai backend`, a config reload and a config write all refuse a `backend_module` over
an in-memory hooks registry, naming `HOOKS_REDIS_URL`, because the backend process
matches the events it raises (an expired ask, for one) against its own registry,
which holds none of the hooks registered through the server.

### Fleet restarts and the server's graceful-shutdown cap

A settings change that needs a restart (a profile apply, an env write, a config reload)
rolls a fleet restart inside the request that made the change: each other worker that
needs it is restarted in turn, and the request answers once the roll ends. A worker whose
reload was refused at its epoch build (the build discarded before its swap) is not
restarted: the stored config did not build on it and a restart may fail the same way, so it
keeps serving its current config and the response names it in `awaiting_respawn`; fix the
stored config, or the cause the build error names, before restarting it. For each worker the
roll waits the drain that worker announces as it leaves, plus the start-and-rejoin step (the
app's declared drain budget, `TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS`, 10 s by default). The
drain a worker announces covers its whole stop: its runtime's stop phase, then the app's
lifespan shutdown, whose steps run one after another within the bounds the app's modules
declare — the hook-run attempts twice `HOOKS_RUN_SHUTDOWN_GRACE_SECONDS` (their grace to
finish, then the cancelled ones' grace to settle their jobs), the credential refreshes'
close wait (30 s, for a refresh to finish its write-back), the states outbox
`STATES_OUTBOX_SHUTDOWN_GRACE_SECONDS`, and the tool-run supervisors
`TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS`: 100 s at the defaults. A `tai serve` worker announces
its graceful-shutdown cap plus those bounds (110 s at the defaults); a `tai backend` worker
announces its backend's own drain bound — for the shipped backends, its task timeout (arq:
its job-completion wait) plus a 30 s grace — plus the same bounds. The leaving worker's
census row reads `recycling` with that announced drain from its departure decision, or the
first stop signal, `SIGTERM` or `SIGINT`, of a stop decided outside it, until the stop
deletes the row once its shutdown handlers are over (before it tears down its resources), so
the roll sees the drain for the whole stop and a roll reaching a worker already stopping
waits that drain out with no second signal. A roll over a backend
therefore takes as long as that backend's in-flight jobs need, which is often far longer
than a minute.

A change made under a bound fixed before it started cannot wait out such a roll: a change
run as a backend job (a queued or synchronous tool task, a schedule or hook forwarded to
the backend, a callback's tool) runs under the job's budget, and a call from a live caller (an
MCP tool call, a `POST /api/run-tool` call such as `tai tools run`, an agent run, a
conversation turn; a preset run through any of these) runs under the turn budget when
`TAI_TURN_TIMEOUT_SECONDS` is set. Such a change hands its roll to the worker it runs for —
the `tai backend` worker that ran the job, or the worker serving the call — and answers at
once with `roll_deferred: true`, `roll_deferred_to` naming that worker, and no roll rows.
It does so even when that worker's own restart is the only one: that restart waits for the
worker's other changes in flight, which the bound cannot hold either. That worker carries
the roll out on its own, outside every bound: it waits each worker's drain and step as
above and, when the roll converged and it needs its own restart, restarts itself last. The roll's outcome is in
that worker's log. A hand-off that fails fails the change loudly; what the change wrote stays
written. The hand-off's error names the worker and whether the reload reached the bus. When the
worker is already leaving, or the worker serving the call could not take the roll, the roll did
not start; when its answer does not tell (the op failed on the worker over the bus, the bus
unreachable or failing, no answer came), the roll may have been sent to that worker and whether
it started is unknown, and that worker's log and the fleet census tell. A backup import's
config section carries the roll in its report: its `errors` name the change's failure, its
`fanout` error names the roll's, a roll that did not start lists in `awaiting_respawn` the
workers whose reload reported a moved key, and one whose answer is unknown is left to that
worker — `roll_deferred: true`, `roll_deferred_to` naming it, no roll rows and an empty
`awaiting_respawn`. An env or manifest write, a profile apply and a config soft restart answer
with the error's text alone and no roll fields: `POST /api/run-tool` as a structured 500
`{"error": ...}`, an MCP tool call as the tool's error, a backend job as the job's error. That
text names the hand-off's failure when the change's own reload and broadcast succeeded; when
one of them failed too, the text names that failure, and the hand-off's failure is in the
worker's log. A marketplace operation whose hand-off fails answers with no roll fields: its
error says the config changes landed but the fleet recycle roll they owe did not complete,
naming only the failure's type, whose text is in the worker's log. A change with no bound (an
HTTP request outside those doors, a live call with no turn budget) rolls inside the change as
described above.

`tai serve` caps the server's own stop phase at `APP_ARGS_TIMEOUT_GRACEFUL_SHUTDOWN` seconds
(10 by default; the `--timeout-graceful-shutdown` extra-arg overrides it): after that cap,
uvicorn cancels the requests still running. The cap does not bound the app's lifespan shutdown,
which runs after it within the bounds above. The worker that made the change never cuts its own
roll this way — its own restart runs only after its reply is sent. But a worker that is stopped
from outside while its request still rolls (a rolling deploy, a scale-down, the process
manager's stop) cancels the roll at that cap: the caller gets no reply, and the workers after
the cut are not restarted by that roll. When settings changes that restart the fleet run during
such stops, set the cap to at least the longest roll the deployment expects, and give the
process manager's own stop timeout room above the cap plus the app's shutdown bounds.

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
tai conversations config-set agent assistant --multichannel \
  --greeting-template 'Hi! Pair another channel with {pairing_code}'
tai conversations config-list                  # GET /api/conversation-configs
tai conversations config-get agent assistant   # GET .../{target_kind}/{target_name}
tai conversations config-delete agent assistant # DELETE .../{target_kind}/{target_name}
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
`channel-web` `?tai_pair=` URL) around it. Opt the module in with a manifest
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
the tool's kwargs with a `start_expr` (jq) and the result to the reply with a
`reply_expr` (jq). The payload the expr runs over is
`{message, sender, our_identity, channel, route}` — `our_identity` and `channel`
are the route's, `route` is the name of the route the turn entered through (so a
tool bound to several routes can tell them apart), `sender` is the conversation
address — with the run's parked interactions bound as `$parked`. Map those onto the
tool's own parameter names and pull `.code` out of the result:

```bash
tai conversations create pair-mint --door channel --target-kind tool \
  --target-name get_pairing_code --execution-key svc \
  --channel telegram --identity <bot-id> \
  --start-expr '{channel: .channel, our_identity: .our_identity, sender: .sender}' \
  --reply-expr '.code'
```

`start_expr` must emit exactly one JSON object (or null to start nothing); `reply_expr` must emit null or a
string — here the raw code — so the route replies with just the code. The tool
mints for the conversation named by `(channel, our_identity)`, whose resolved
target must itself have `multichannel` on.

`reply_expr` maps a RETURNED result, whatever keys it carries. A target that fails
RAISES `RunTerminalFailed(outcome)`: the turn fails regardless of the mapping, the
participant reads the target's own reply when the failure carries one
(`RunTerminalFailed(outcome, reply="...")`), else the route's `reply_texts.error`, else
the built-in line, and the opaque outcome rides the record's `error` detail. A run that
PARKS (returns a `SuspendedInteraction`) is neither mapped nor failed: the turn ends
silently and the reply delivers out of band when the run resumes past the pause.

A route's `reply_texts` catalogue carries its own words, in the line's language, for the
platform's fixed participant replies: `error` (a failed turn with no target reply),
`slow_down` (an address over the turn cap), the pairing replies `pairing_linked`,
`pairing_unlinked`, `pairing_not_linked`, `pairing_code_invalid` and `pairing_code` (the
`/link` reply, a template that must reference `{pairing_code}` and may reference
`{expires_at}`), and the inbound refusal notices `rejected_unsupported_type`,
`rejected_too_large` and `rejected_could_not_receive`. An unset member sends the built-in
English reply.

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

- Getting started & install: https://docs.tai42.ai/getting-started/installation
- Quickstart: https://docs.tai42.ai/getting-started/quickstart
- Concepts: https://docs.tai42.ai/concepts
- Guides: https://docs.tai42.ai/guides
- Built-in tools & extensions: https://docs.tai42.ai/concepts/tools-and-extensions
- HTTP API reference: https://docs.tai42.ai/reference/api
- CLI reference: https://docs.tai42.ai/reference/cli
- Python SDK reference: https://docs.tai42.ai/reference/python-sdk

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
