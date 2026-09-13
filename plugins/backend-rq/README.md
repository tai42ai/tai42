# tai42-backend-rq

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

RQ execution backend for the TAI ecosystem: background tool runs and recurring
schedules over Redis.

It implements the `tai42_contract.backend.Backend` contract — one strategy object
that launches the worker runtime (`worker` / `beat` / `dashboard`) and executes
the work its workers pull from the broker. Fleet propagation of config changes
is not a backend concern: it is the app's own worker bus, internal to the
skeleton, which a backend-runtime process receives fleet ops through exactly
like a serving HTTP worker.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An
execution `Backend` is "how work runs beyond the request" — a pluggable
strategy the runtime uses to queue tool executions on a worker fleet. Fanning
control operations (manifest updates, config/tool/MCP reloads) out across every
worker is the skeleton's worker bus, not the backend — a backend-runtime process
just joins that bus like any other worker. This package is one such backend
(RQ over Redis); any package can back the same contract, so this repo is this
provider's own full doc home, and the documentation site covers the
platform-level story:

- Backend concept: https://tai42.ai/concepts/backends
- Build a backend (author guide): https://tai42.ai/guides/authors/backend
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+** and a reachable Redis. Install from PyPI into the
environment that runs the server:

```bash
uv add tai42-backend-rq
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/backend-rq
```

## Discovery

The host manifest names this package as its backend module:

```yaml
backend_module: tai42_backend_rq
```

Importing the package registers, as an import side-effect on the global
`tai42_app` handle:

- **`RqBackend`** via `@tai42_app.backends.register_backend` — the `launch`
  entrypoint (`worker` / `beat` / `dashboard`).
- **The `backend_*` tool surface** via `@tai42_app.tools.tool` (see below).
  `backend_list_schedules`, `backend_delete_schedule`,
  `backend_export_schedules`, and `backend_import_schedules` are the marker
  tools the host probes for scheduling availability and uses for the backup
  round-trip.
- **Three BACKEND tool extensions** via `@tai42_app.extensions.extension`:
  `sync_task` (queue and wait for the result), `async_task` (queue and return
  the job id), and `schedule_task` (register a recurring interval/crontab
  schedule). Each mints a `<tool>_<extension>` branch tool whose queued job
  dispatches back onto the original tool by name.

## Runtime

`launch(args)` selects the process role from the first argument:

- `worker [--redis-url URL] [-n NAME] [--loglevel LEVEL] [--burst] [--results-ttl N] [--pool prefork|solo|gevent]`
  — runs an RQ worker. `prefork` (default) forks a monitored work-horse per
  job; `solo` runs jobs in-process; `gevent` runs jobs in-process on green
  threads. `--results-ttl` sets the worker's default result TTL. The work
  loop runs on a worker thread so the process's event loop stays responsive
  (the app's worker-bus subscription lives on it, delivering fleet ops);
  SIGTERM/SIGINT request a warm shutdown (finish the current job), a repeated
  signal escalates to RQ's cold shutdown.
- `beat [rqscheduler options]` — runs the recurring-job scheduler
  (`rq-scheduler`).
- `dashboard [rq-dashboard options]` — runs the RQ web dashboard.

## Fleet control

Fleet propagation of config changes (manifest updates, MCP/tool/config reloads)
is carried by the skeleton's internal worker bus, which every process — this
backend runtime included — joins through the app context's single long-lived
bus subscription; this backend ships no control plane of its own.

## Fork safety

On the prefork pool the worker shuts down the monitoring writer before the
first fork (the contract's fork-safe evict — the vendor client's background
threads do not survive `fork()`), and on macOS it disables `urllib` system
proxy detection, which deadlocks in forked children during SSL setup. An
`os.register_at_fork` hook — installed on *every* pool, since `solo` and
`gevent` still spawn the `rq-scheduler` child — repeats the evict in each
forked child, which then rebuilds a clean client lazily, and resets the
fork-gate state the child inherited. Every step logs loudly; telemetry is
never silently disabled.

The fork gate is the mutual exclusion between this worker and a config
reload. A reload pops the manifest modules out of `sys.modules` and re-imports
them; a child forked during that window inherits a held `importlib` lock whose
owner thread no longer exists and hangs forever on its first import. So:

- **prefork** holds the gate across the `fork()` instant only — a child forked
  outside the window is immune however long its job runs. A job that arrives
  mid-reload waits for the re-import to finish, up to 120s, before forking.
- **solo / gevent** run jobs in-process, with no inherited snapshot to protect
  them, so the gate is held for the whole job. A config reload arriving while
  one is running therefore waits for it, up to 30s.

Both waits are bounded and proceed loudly (ERROR) rather than deadlocking if
the budget is exhausted.

## Tools

Task/worker tools: `backend_task_status`, `backend_task_result` (raises the
task's stored failure — the persisted traceback text — when the task FAILED),
`backend_cancel_task`, `backend_active_tasks` (keyed by worker name),
`backend_reserved_tasks` (keyed by queue name — RQ reserves work per queue),
`backend_scheduled_tasks` (keyed by job id, value carries `next_run_at_ts`),
`backend_worker_stats` (keyed by worker name), `backend_worker_queues`,
`backend_ping_worker`, `backend_list_active_workers`.

Schedule tools: `backend_schedule_exists`, `backend_get_schedule`,
`backend_list_schedules` (canonical row keys `name` / `enabled` /
`next_run_at_ts` / `next_run_at_iso`, plus the `meta` extra; `enabled` is
always true because RQ has no disabled-schedule state),
`backend_delete_schedule`, `backend_enable_schedule` (delegates to run-now and
returns that op's `queued` / `not_found` statuses),
`backend_disable_schedule` (delegates to delete and returns that op's
`deleted` / `not_found` statuses — RQ has no disabled-schedule state),
`backend_run_schedule_now`, `backend_update_schedule`,
`backend_export_schedules`, `backend_import_schedules` (per-row errors
surfaced as `{"index", "name", "error"}`, never swallowed).

Not supported on RQ (raise `NotImplementedError`): `backend_registered_tasks`,
`backend_list_failed_tasks`.

## Configuration

Env group `RQ_` (a `tai42_kit` settings class, cached and reset on live reload):

| Env var | Default | Meaning |
| --- | --- | --- |
| `RQ_REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL (broker and results). |
| `RQ_RQ_PREFIX` | `rq:` | Key prefix RQ uses for its Redis structures. |
| `RQ_MANIFEST_KEY` | `MANIFEST_KEY` | Env key the worker CLI stores the manifest JSON under (inherited by forked work-horses). |
| `RQ_TASK_TIMEOUT` | `300` | Seconds a `sync_task` dispatch waits for its job's result. |
| `RQ_TOOL_NAME_ARG` | `backend_tool_name` | Kwarg carrying the target tool name in a queued job. |

The defaults of `RQ_MANIFEST_KEY`, `RQ_TASK_TIMEOUT`, and `RQ_TOOL_NAME_ARG`
deliberately agree with the host's generic `BACKEND_` settings group, so the
tool-dispatch seam meets without configuration.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
