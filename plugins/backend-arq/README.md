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

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

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
`sync_task` / `schedule_task` / `async_task` BACKEND-kind tool extensions, and
a shutdown hook closing the shared ArqRedis pool. Name the package in your
manifest's `backend_module` field:

```yaml
backend_module: tai42_backend_arq
```

Start a worker through the host's backend CLI; everything after `worker` is
this backend's own option surface (see `tai42_backend_arq.worker.main`):

```bash
tai backend worker --max-jobs 10 --job-timeout 300
```

## Configuration

Settings are read from the `ARQ_` environment group (see `ArqSettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `ARQ_REDIS_URL` | `redis://localhost:6379/0` | Redis connection for the queue and schedules |
| `ARQ_REDIS_MAX_CONNECTIONS` | — | Optional cap on the pool's connections |
| `ARQ_CALLBACK_TIMEOUT` | `5` | Seconds a callback job waits for its predecessor to complete |
| `ARQ_MANIFEST_KEY` | `MANIFEST_KEY` | Name of the env var the host exports the manifest under for worker processes |
| `ARQ_TASK_TIMEOUT` | `300` | Seconds a synchronous branch tool waits for its queued job's result |
| `ARQ_TOOL_NAME_ARG` | `backend_tool_name` | kwargs key carrying the target tool name into a queued execution |

`ARQ_MANIFEST_KEY`, `ARQ_TASK_TIMEOUT`, and `ARQ_TOOL_NAME_ARG` mirror the
host's backend settings defaults — both sides
must agree on these values without sharing code, so override them only in
lockstep with the host.

Worker CLI options (after the `worker` subcommand): `--redis-url` (defaults to
`ARQ_REDIS_URL`), `--burst`, `--keep-result` (default `3600`; `0` disables
result retention), `--queue-name`, `--max-jobs` (default `10`),
`--job-timeout` (default `300`), `--poll-delay` (default `0.5`), `--max-tries`
(default `5`), `--health-check-interval` (default `60`). The worker runs with
arq's `allow_abort_jobs` enabled — task cancellation and schedule
replace/delete rely on abort processing.

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
(`backend_list_schedules` — canonical row keys `name` / `enabled` /
`next_run_at_ts` / `next_run_at_iso` plus the `schedule` / `target` / `args` /
`kwargs` extras —, `backend_get_schedule`, `backend_delete_schedule`,
`backend_enable_schedule`, `backend_disable_schedule`,
`backend_run_schedule_now`, `backend_schedule_exists`,
`backend_update_schedule`) operate on those hashes, and
`backend_export_schedules` / `backend_import_schedules` round-trip portable
schedule records for backup (per-row import errors surfaced as
`{"index", "name", "error"}`, never swallowed). Schedules accept an interval
(seconds or an `{"type": "interval", ...}` mapping) or a 5-field crontab
(string or mapping).

## Task execution

The `sync_task` / `async_task` extension branches dispatch the wrapped tool to
the queue via the `tool_execution` job; a `sync_task` wait re-raises a failed
job's stored failure as `TaskFailedError` (aborted jobs included). Task
options exposed on every branch:
`countdown` (defer by seconds), `eta` (ISO datetime; defer until then),
`expires` (seconds the queued job stays runnable), and `callback_kwargs` — a
callback schema chained via a `callback_job` that runs over the primary job's
result (jq condition gate, jq expression transform, optional follow-up tool).
`schedule_task` registers a recurring schedule (see below).

## Tools

Task/worker tools use arq's public `Job` status/result API:
`backend_task_status`, `backend_task_result` (timeout-aware; a task's stored
failure re-raises as `TaskFailedError` carrying the original exception's type,
`repr`, and traceback text — result payloads describe an unserializable value
in a tagged JSON shape the deserializer revives on read), `backend_cancel_task`
(a stored abort replays as arq's confirmed-abort verdict; stored failures are
reported with their detail), `backend_active_tasks` (a flat
job-id-keyed map — arq has no per-worker attribution), `backend_reserved_tasks`
(a flat list of due job ids), `backend_scheduled_tasks` (job id → due time in
epoch milliseconds), `backend_list_failed_tasks` (failed/aborted results
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
