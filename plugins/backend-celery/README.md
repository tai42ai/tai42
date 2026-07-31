# tai42-backend-celery

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Celery execution backend for the TAI ecosystem. It implements the
`tai42_contract.backend.Backend` surface — launching the worker runtime
(`worker` / `beat` / `flower`). Fleet config propagation is not a backend
concern: a backend-runtime process receives worker-bus ops (`reload_config`,
`reload_mcp`, …) through the skeleton's own worker bus, exactly like a serving
HTTP worker. This backend adds one thing on top of that: after a bus op applies,
it re-forks this worker's prefork pool so the forked children re-inherit the
updated tool registry (see [Live reload and the prefork pool](#live-reload-and-the-prefork-pool)).
It also ships the `sync_task` / `schedule_task` / `async_task` tool extensions
(execute any tool through the queue) and the `backend_*` tool surface, including
the scheduling marker tools the host's schedules API and backup round trip
depend on.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An
execution `Backend` is the runtime's execution strategy — one pluggable object
that runs the background worker fleet (the processes that pull and execute
enqueued work). Fleet config propagation is not a backend concern; live-reload
operations reach each worker through the skeleton's own worker bus. This
package is one such backend
(Celery); siblings can back the same contract with other queues. The
documentation site covers the platform-level story:

- Backend & fleet-control concept: https://tai42.ai/concepts/backends
- Build an execution backend (author guide): https://tai42.ai/guides/authors/backend
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Backend` ABC, the
`CallbackSchema` field shape, `Manifest`, `ExtensionKind`, and the `tai42_app`
handle) and `tai42-kit` (settings base + cache, the pooled Redis client,
schedule normalization, signature helpers, and the jq compiler). Beyond those
it depends on its broker stack: `celery`, `kombu`, `celery-redbeat`, `redis`,
`celery-pydantic`, `flower`, and `makefun` — plus `fastmcp` (the platform's
tool substrate) for FastMCP-context handling when composing dispatch-branch
signatures.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-backend-celery
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/backend-celery
```

## Discovery

The host discovers this backend by **importing its package** — importing
`tai42_backend_celery` registers everything through the global `tai42_app` handle
as a side-effect (there is no entry-point): the `CeleryBackend`
(`tai42_app.backends.register_backend`), the Celery application with its
pool-child fork-safety hooks, the `backend_*` tool surface, and the `sync_task`
/ `schedule_task` / `async_task` BACKEND-kind tool extensions. Name the package
in your manifest's `backend_module` field:

```yaml
backend_module: tai42_backend_celery
```

The worker runtime is started through the host's backend launcher with one of:

```bash
tai backend worker [celery worker options...]
tai backend beat
tai backend flower
```

## Fleet ops and the worker bus

Fleet config propagation lives in the skeleton's worker bus, internal to the
app: a backend-runtime process joins that bus through the app's single
long-lived subscription and receives each op (`reload_config`, `reload_mcp`, …)
exactly like a serving HTTP worker — this backend carries no control-plane of
its own. Its one obligation on top of the bus is the prefork-pool turnover:
after every applied MUTATING op the backend re-forks this worker's pool (see
[Live reload and the prefork pool](#live-reload-and-the-prefork-pool)).

The worker runs on a background thread so this process's event loop stays alive
to keep reading the bus subscription; `launch` installs SIGTERM/SIGINT handlers
that request a warm (then cold) Celery shutdown, since Celery skips its own
signal handlers off the main thread.

## Task execution

The `sync_task` / `async_task` extension branches dispatch the wrapped tool to
the queue via the `celery.tool_execution` task (autoretry on
connection/timeout errors with jittered exponential backoff, never on
`ValueError`; max 5 retries). Task options exposed on every branch: `queue`,
`countdown`, `priority`, `retry`, `routing_key`, `expires`, `eta`, and
`callback_kwargs` — a callback schema chained via a Celery `link` that runs
`celery.callback_task` on the result (jq condition gate, jq expression
transform, optional follow-up tool).

`schedule_task` persists a RedBeat entry (interval seconds or 5-field
crontab). Beat must run with the RedBeat scheduler, which `launch("beat")`
configures automatically.

Worker fork safety: each freshly forked pool child evicts the monitoring
vendor client through the contract's fork-safe `writer.shutdown()` (logged,
never a silent telemetry disable) and, at child exit, flushes buffered spans
and closes its task loops' pooled clients. On macOS a warning names the
platform's fork/DNS hazard; prefer the `solo` or `threads` pool there.

## Tools

Task/worker tools: `backend_ping_worker`, `backend_list_active_workers`,
`backend_task_status`, `backend_task_result` (timeout-aware; re-raises task
exceptions), `backend_cancel_task`, `backend_active_tasks`,
`backend_reserved_tasks`, `backend_scheduled_tasks`,
`backend_registered_tasks`, `backend_worker_stats`, `backend_worker_queues`.
The fleet-view tools return Celery's inspect shape — a mapping of worker name
to that worker's entries — so the keying is per worker.
`backend_list_failed_tasks` raises `NotImplementedError` — Celery keeps no
queryable failed-task index.

Schedule tools (RedBeat): `backend_list_schedules` (canonical row keys `name`
/ `enabled` / `next_run_at_ts` / `next_run_at_iso`), `backend_get_schedule`,
`backend_schedule_exists`, `backend_enable_schedule`,
`backend_disable_schedule`, `backend_run_schedule_now`,
`backend_update_schedule`, `backend_delete_schedule`, plus the backup round
trip `backend_export_schedules` / `backend_import_schedules` (portable
`ScheduleRecord` rows; upsert by name; per-row errors surfaced as
`{"index", "name", "error"}`, never swallowed).

## Configuration

Settings are read from the `CELERY_` environment group (see `CelerySettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `CELERY_BROKER_URL` | `amqp://localhost:5672//` | Celery broker URL — task queue and Celery pool control |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery result backend URL |
| `CELERY_REDBEAT_REDIS_URL` | `redis://localhost:6379/0` | Redis URL for the RedBeat schedule store |
| `CELERY_REDBEAT_KEY_PREFIX` | `redbeat:` | RedBeat key prefix (schedule zset is `redbeat::schedule`) |
| `CELERY_BEAT_MAX_LOOP_INTERVAL` | `60` | Maximum seconds between beat scheduler ticks |
| `CELERY_TOOL_NAME_ARG` | `backend_tool_name` | Kwarg name the dispatch injects the target tool into |
| `CELERY_TASK_TIMEOUT` | `300` | Seconds a `sync_task` branch waits for the result |
| `CELERY_MANIFEST_KEY` | `MANIFEST_KEY` | Env var carrying the live manifest JSON (prefork inheritance) |
| `CELERY_WORKER_CONCURRENCY` | `1` | Prefork pool size — capped at one child by default (see below) |

### Live reload and the prefork pool

After the worker bus applies a MUTATING op in this process, the backend re-forks
this worker's prefork pool so its children re-inherit the parent's updated tool
registry, and it CONFIRMS the restart rather than assuming it: it polls `stats`
until every pre-restart child PID is gone and the pool is back to full size. A pool
that does not fully re-fork fails the op loudly, naming the worker — turning this
worker's bus reply into `failed` rather than reporting an applied op behind stale
children. The query op `list_failed_mcps` never re-forks the pool.

The cap of **one pool child** is what makes that confirmation achievable. Celery's
`pool_restart` is a *soft* restart: it arms a per-child restart sentinel and returns
as soon as the restart is armed, before the pool has finished re-forking. Billiard's
idle children recycle on their ~1s poll wakeup, but a child that is *busy* (mid-task)
cannot recycle until its task completes — so a wider pool under load can leave children
un-recycled past the confirmation budget. The `on_fleet_op_applied` successor confirms
turnover (all pre-restart PIDs gone and the pool back to full size) and raises
otherwise, which is what a single child keeps fully confirmable. Raise
`CELERY_WORKER_CONCURRENCY` only if you do not live-reload; a pool that is both wide and
live-reloadable needs a non-forking pool (`threads`/`gevent`) or a full worker-process
respawn on reload.

The turnover runs inside the op apply, before this worker's terminal reply is sent, and
the publisher waits only its bus apply window for that reply. So the confirmation budget
is derived from `TAI_BUS_APPLY_TIMEOUT` (the same knob the bus reads, default `30s`) and
sits a small margin under it: the confirm or raise reaches the publisher before its report
cut, so a stalled turnover is recorded as a truthful `failed` rather than a `timed_out`
guess. Raising `TAI_BUS_APPLY_TIMEOUT` raises the turnover budget under it.

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
