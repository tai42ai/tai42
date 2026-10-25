# tai42-backend-arq

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

An [arq](https://arq-docs.helpmanual.io/) execution backend for the TAI
ecosystem. It implements the `tai42_contract.backend.Backend` surface — the worker
runtime (`launch`) — and layers the platform's background-execution features
over arq: queued and awaited tool runs, recurring schedules (interval or
crontab) with export/import backup, and result-chaining callbacks. Fleet
propagation of config changes is not this backend's concern: a backend-runtime
process receives fleet ops through the app's own internal worker bus, exactly
like a serving HTTP worker.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An
execution `Backend` is "how work runs beyond the request" — a pluggable
strategy the runtime uses to queue tool executions on a worker fleet. Fanning
control operations (manifest updates, config/tool/MCP reloads) out across every
worker is the skeleton's worker bus, not the backend — a backend-runtime process
just joins that bus like any other worker. This package is one such backend (arq
over Redis); any package
can back the same contract, so this repo is this provider's own full doc home,
and the documentation site covers the platform-level story:

- Backend concept: https://tai42.ai/concepts/backends
- Build a backend (author guide): https://tai42.ai/guides/authors/backend
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Backend` ABC,
`CallbackSchema`, `Manifest`, and the `tai42_app` handle) and `tai42-kit[jq]`
(settings base, schedule normalization, signature helpers, jq). Beyond those it
depends on the broker stack — `arq`, `croniter`, `orjson`, `makefun`, `click` —
plus `fastmcp` (the platform's tool substrate) and `pydantic` /
`pydantic-settings` / `pydantic-core`.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-backend-arq
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/backend-arq
```

## Discovery

The host discovers this backend by **importing its package** — importing
`tai42_backend_arq` registers everything through the global `tai42_app` handle as
a side-effect (there is no entry-point): the `ArqBackend`
(`@tai42_app.backends.register_backend`), the `backend_*` tool surface, the
`sync_task` / `schedule_task` / `async_task` BACKEND-kind tool extensions. The
enqueue side (enqueues, job status and results, schedule hashes) runs over the
platform's pooled Redis client, so its connections are pooled per configuration
generation and closed when that generation retires. Name the package in your
manifest's `backend_module` field:

```yaml
backend_module: tai42_backend_arq
```

A config that registers a task backend also needs the worker bus and a shared
hooks registry: `tai serve`, `tai backend`, a config reload and a config write all
refuse it unless `TAI_BUS_REDIS_URL` and `HOOKS_REDIS_URL` are set (each falls back
to `TAI_DEFAULT_REDIS_URL`). The backend process matches the events it raises
against its own hooks registry, so an in-memory one would run none of the hooks
registered through the server.

Start a worker through the host's backend CLI; everything after `worker` is
this backend's own option surface (see `tai42_backend_arq.worker.main`):

```bash
tai backend worker --max-jobs 10
```

## Configuration

Settings are read from the `ARQ_` environment group (see `ArqSettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `ARQ_REDIS_URL` | `redis://localhost:6379/0` | Redis connection for the queue and schedules |
| `ARQ_REDIS_MAX_CONNECTIONS` | — | Optional cap on the pool's connections |
| `ARQ_QUEUE_NAME` | `arq:queue` | The arq queue key the host enqueues to and a worker started without `--queue` consumes; a `--queue <label>` worker consumes `<ARQ_QUEUE_NAME>.<label>`. Every process of one stack sets the same value. Stacks sharing one Redis DB set distinct values, and no stack's value is another stack's value followed by `.<label>`. |
| `ARQ_RETRY_ATTEMPTS` | `0` | Times a failed Redis connect or command is retried, each retry logged as a warning; `0` makes one attempt |
| `ARQ_RETRY_ON_TIMEOUT` | `false` | With `ARQ_RETRY_ATTEMPTS` at `0`, retry a timed-out command once at once |
| `ARQ_SOCKET_TIMEOUT` | — | Seconds a Redis command may wait on its socket; redis-py's default when unset |
| `ARQ_SOCKET_CONNECT_TIMEOUT` | `1` | Seconds a Redis connect may take |
| `ARQ_CALLBACK_TIMEOUT` | `5` | Seconds a callback job waits for its predecessor to complete |
| `ARQ_MANIFEST_KEY` | `MANIFEST_KEY` | Name of the env var the host exports the manifest under for worker processes |
| `ARQ_TASK_TIMEOUT` | `300` | The dispatch job budget in seconds: the longest a job may run on the worker (the `--job-timeout` default), and how long a synchronous branch tool waits for its queued job's result |
| `ARQ_TOOL_NAME_ARG` | `backend_tool_name` | kwargs key carrying the target tool name into a queued execution |

`ARQ_MANIFEST_KEY`, `ARQ_TASK_TIMEOUT`, and `ARQ_TOOL_NAME_ARG` mirror the
host's backend settings defaults — both sides
must agree on these values without sharing code, so override them only in
lockstep with the host. `ARQ_QUEUE_NAME` must agree the same way: the host
enqueues to it and records every labelled queue under `<ARQ_QUEUE_NAME>:queues`,
and each worker consumes it or a queue derived from it.

`ARQ_REDIS_URL` and `ARQ_REDIS_MAX_CONNECTIONS` fall back to the shared
`TAI_DEFAULT_REDIS_URL` / `TAI_DEFAULT_REDIS_MAX_CONNECTIONS` when unset.

Every Redis connection of this backend — the enqueue side and the worker — is
built from these settings. The worker's connect at start follows
`ARQ_RETRY_ATTEMPTS`, so with the default `0` a worker started before Redis is
reachable fails at once. A running worker rides a Redis outage only within that
same retry: arq's poll loop has no reconnect of its own, so an outage longer
than the retry ends the worker.

Worker CLI options (after the `worker` subcommand): `--redis-url` (defaults to
`ARQ_REDIS_URL`; the other connection settings still apply), `--burst`, `--keep-result` (default `3600`; `0` disables
result retention), `--queue LABEL` (consume `<ARQ_QUEUE_NAME>.<label>` instead of
`ARQ_QUEUE_NAME`; one label per worker, letters, digits, `_` or `-`; any other
value is refused before the worker starts), `--max-jobs` (default `10`),
`--job-timeout` (defaults to `ARQ_TASK_TIMEOUT`), `--poll-delay` (default `0.5`),
`--max-tries` (default `5`), `--health-check-interval` (default `60`). The worker
runs with arq's `allow_abort_jobs` enabled — task cancellation and schedule
replace/delete rely on abort processing. A dispatched tool runs once: the backend
never retries a tool body (a tool that wants retries declares a `ToolRetryPolicy`).
arq itself re-runs a job cut by a cold stop (`CancelledError`), bounded by
`--max-tries` — arq's own delivery semantics.

## Fleet ops

Config-change propagation across the worker fleet (manifest updates,
config/tool/MCP reloads) is the app's own internal worker bus, not a backend
concern — a backend-runtime process receives those ops through the app's bus
subscription exactly like a serving HTTP worker. See the skeleton's worker-bus
concept: https://tai42.ai/concepts/worker-bus

## Schedules

Recurring runs live in per-schedule Redis hashes (`arq:schedule:{name}`)
driven by a self-rescheduling `task_scheduler` job; a startup watchdog
restarts schedules whose pending job was lost. The schedule tools
(`backend_list_schedules` — the contract's `ScheduleListRow`: `name` / `tool` /
`cadence` / `enabled` / `next_run_at_ts` / `next_run_at_iso` / `error`, never the
stored arguments —, `backend_get_schedule`, `backend_delete_schedule`,
`backend_enable_schedule`, `backend_disable_schedule`,
`backend_run_schedule_now`, `backend_schedule_exists`,
`backend_update_schedule`) operate on those hashes, and
`backend_export_schedules` / `backend_import_schedules` round-trip the
contract's `ScheduleRecord` rows for backup (per-row import errors surfaced as
`{"index", "name", "error"}`, never swallowed). A schedule's cadence is the
contract's typed `ScheduleCadence` (`tai42_contract.backend.scheduling`); the hash
stores its JSON dump in the `schedule` field. arq cannot honour a relative
interval, so `relative: true` is refused on create, update and import.

### Upgrading: reset the schedule store

The `schedule` field's stored format is the typed cadence. A hash written in the
earlier format makes the schedule tools (list, export, get, update) raise
`ScheduleStoreFormatError` naming the schedule; arq keeps firing such a hash from
its `cron_or_interval` field. Reset the store and re-create the schedules:

```bash
redis-cli --scan --pattern 'arq:schedule:*' | xargs redis-cli del
```

The pattern is `ArqSettings.arq_schedule_pattern` (`arq:schedule:*`).

## Task execution

The `sync_task` / `async_task` extension branches dispatch the wrapped tool to
the queue via the `tool_execution` job; a `sync_task` wait re-raises a failed
job's stored failure as `TaskFailedError` (aborted jobs included). Task
options exposed on every branch:
`countdown` (defer by seconds), `eta` (ISO datetime; defer until then),
`expires` (seconds the queued job stays runnable), `queue` (a worker queue
label; see below), and `callback_kwargs` — a
callback schema chained via a `callback_job` that runs over the primary job's
result (jq condition gate, jq expression transform, optional follow-up tool).
`schedule_task` registers a recurring schedule (see above).

`queue` sends the job to the worker started with `--queue <label>`: a label of
letters, digits, `_` and `-` derives the queue `<ARQ_QUEUE_NAME>.<label>`, and
any other value raises `ValueError` before anything is written. Unset, the job
goes to `ARQ_QUEUE_NAME`. A callback job runs on the same queue as its
predecessor. A job sent to a queue no worker consumes waits: a `sync_task`
raises `TimeoutError` after `ARQ_TASK_TIMEOUT`, and an `async_task` id stays
`queued`. A tool with its own `queue` parameter cannot take a `sync_task` or
`async_task` branch. Schedules carry no queue: the control job and every fire
run on `ARQ_QUEUE_NAME`, so recurring schedules need a worker without a label.

## Tools

Task/worker tools use arq's public `Job` status/result API; a tool given a job
id reads it on the queue that holds it:
`backend_task_status`, `backend_task_result` (timeout-aware; a task's stored
failure re-raises as `TaskFailedError` carrying the original exception's type,
`repr`, and traceback text — result payloads describe an unserializable value
in a tagged JSON shape the deserializer revives on read), `backend_cancel_task`
(a stored abort replays as arq's confirmed-abort verdict; stored failures are
reported with their detail), `backend_active_tasks` (a flat
job-id-keyed map — arq has no per-worker attribution), `backend_reserved_tasks`
(due job ids keyed by queue: `ARQ_QUEUE_NAME` and every labelled queue a task
was sent to, an empty queue as `[]`), `backend_scheduled_tasks` (job id → due
time in epoch milliseconds, across every queue), `backend_list_failed_tasks` (failed/aborted results
within the keep-result window, as `{"task_id", "error"}` rows carrying the
stored failure detail). Capabilities arq has no reliable data model
for raise `NotImplementedError` loudly: `backend_registered_tasks`,
`backend_worker_stats`, `backend_worker_queues`, `backend_ping_worker`,
`backend_list_active_workers`.

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
