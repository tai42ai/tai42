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

- Backend concept: https://docs.tai42.ai/concepts/backends
- Writing a backend (section of the backend concept): https://docs.tai42.ai/concepts/backends#writing-a-backend
- Plugin catalog: https://docs.tai42.ai/plugins

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

A config that registers a task backend also needs the worker bus and a shared
hooks registry: `tai serve`, `tai backend`, a config reload and a config write all
refuse it unless `TAI_BUS_REDIS_URL` and `HOOKS_REDIS_URL` are set (each falls back
to `TAI_DEFAULT_REDIS_URL`). The backend process matches the events it raises
against its own hooks registry, so an in-memory one would run none of the hooks
registered through the server.

Importing the package registers, as an import side-effect on the global
`tai42_app` handle:

- **`RqBackend`** via `@tai42_app.backends.register_backend` — the `launch`
  entrypoint (`worker` / `beat` / `dashboard`).
- **The `backend_*` tool surface**, registered by the kit over this backend's
  worker inspection and schedule store as tools of `tai42_backend_rq` (see below).
  `backend_list_schedules`, `backend_delete_schedule`,
  `backend_export_schedules`, and `backend_import_schedules` are the marker
  tools the host probes for scheduling availability and uses for the backup
  round-trip.
- **Three BACKEND tool extensions** via `@tai42_app.extensions.extension`:
  `sync_task` (queue and wait for the result), `async_task` (queue and return
  the job id), and `schedule_task` (register a recurring schedule from the
  contract's typed cadence, `tai42_contract.backend.scheduling`; a relative
  interval is refused loudly — the rq scheduler cannot honour it). Each mints a `<tool>_<extension>` branch tool whose queued job
  dispatches back onto the original tool by name.

The `sync_task` and `async_task` branches take the task options `countdown`,
`eta`, `expires`, `callback_kwargs` and `queue`. `queue` sends the job, and its
callback, to the worker started with `--queue <label>`: a label of letters,
digits, `_` and `-` derives the queue `<RQ_QUEUE_NAME>.<label>`, and any other
value raises `ValueError` before anything is written. Unset, the job goes to
`RQ_QUEUE_NAME`. A job on a queue no worker serves waits: a `sync_task` raises
`TimeoutError` after `RQ_TASK_TIMEOUT`, and an `async_task` id stays queued. A
tool with its own `queue` parameter cannot take a `sync_task` or `async_task`
branch. Recurring schedules carry no queue: `beat` puts them on
`RQ_QUEUE_NAME`, so they need a worker without a label.

A callback runs only when its job succeeds: the callback job is queued on the
job's success, and a callback job that finds its predecessor failed or still
unfinished raises.

A job does not wait for another job: it chains the next one with
`callback_kwargs`. `sync_task` and `backend_task_result` (given a `timeout`) are
for callers outside a job and raise `RuntimeError` inside one, before anything
is queued; `backend_task_result` with no `timeout` reads the current state and
works anywhere.

## Runtime

`launch(args)` selects the process role from the first argument:

- `worker [--redis-url URL] [-n NAME] [--loglevel LEVEL] [--burst] [--results-ttl N] [--pool prefork|solo|gevent] [--queue LABEL]`
  — runs an RQ worker. `prefork` (default) forks a monitored work-horse per
  job; `solo` runs jobs in-process; `gevent` runs jobs in-process too, one at a
  time, with blocking I/O made cooperative so the greenlets a job spawns overlap
  their I/O. `--results-ttl` sets the worker's default result TTL. The worker
  serves one queue: `RQ_QUEUE_NAME`, or with `--queue <label>` the queue
  `<RQ_QUEUE_NAME>.<label>` alone (one label, letters, digits, `_` or `-`; any
  other value is refused before the worker starts); its `countdown` / `eta` jobs
  are moved onto that queue by a worker serving it. The work
  loop runs on a worker thread so the process's event loop stays responsive
  (the app's worker-bus subscription lives on it, delivering fleet ops);
  SIGTERM/SIGINT request a warm shutdown (finish the current job), a repeated
  signal escalates to RQ's cold shutdown.
  A waiting worker that loses Redis keeps reconnecting through RQ's own loop,
  logged at ERROR, outside `RQ_RETRY_ATTEMPTS`; `--redis-url` replaces only the
  URL, the other `RQ_` connection settings still apply.

  On `--pool gevent` the worker must be patched by gevent's own patch
  (`gevent.monkey.patch_all`) before anything else is imported. The worker
  declares that patch as its process prelude, so `tai backend worker` reads its
  configuration, then restarts itself once, in place, with the patch applied
  first: the same process id and the same arguments, and one
  `restarting with process prelude gevent.monkey:patch_all` log line. Every job's
  coroutine then runs on the host's event loop, the one loop the process's OS
  thread runs, beside the process's own work on it (the worker-bus subscription);
  the kit resources bound to that loop (pooled clients, checkpoint savers, stores)
  are the host's, released at its stop. rq's own scheduler, which moves delayed
  (`eta` / `countdown`) jobs onto the queue, runs as a thread of this worker
  process on this pool rather than as the forked child the other pools use; a
  job that holds the CPU stalls the host loop and that thread alike until it
  yields. A job past `RQ_TASK_TIMEOUT` has its
  coroutine cancelled on that loop at the budget, its cleanup run there, and fails
  with rq's `JobTimeoutException`. On `prefork` and `solo` each job runs its
  coroutine on a fresh loop of its own. A job past `RQ_TASK_TIMEOUT` there has
  its coroutine cancelled and its cleanup run on that loop, then fails with
  `JobTimeoutException`; on `prefork` a thread the job awaits (a sync tool's) is
  not waited for and ends on its own. A coroutine that catches the cancel and
  runs on is not ended by the budget: on `solo` and `gevent` it runs on until the
  worker stops reporting its liveness a second past the budget and the probe ends
  the worker (see Liveness), and a stop sent once the budget has fired raises
  nothing into it (see `backend_cancel_task`); on `prefork` rq kills the
  work-horse once the job runs 60 seconds past the budget. As the job ends, the
  kit resources bound to its loop are released, then the tasks left on that loop
  are cancelled and run to their end, and the loop closes.
- `beat [-b/--burst] [-i/--interval SECONDS] [-v/--verbose] [--quiet] [--path PATH] [--pid FILE] [-j/--job-class CLASS] [-u/--url URL]`
  — runs the recurring-job scheduler (`rq-scheduler`'s `Scheduler`, with its
  option names, defaults and meanings), on the same connection settings as the
  worker: the URL comes from `RQ_REDIS_URL` (falling back to
  `TAI_DEFAULT_REDIS_URL`) unless `-u/--url` overrides it. The scheduler loop
  has no reconnect of its own, so the beat rides a Redis outage only within
  `RQ_RETRY_ATTEMPTS`; a longer one ends it. An unknown option is refused
  before anything connects. Every job it moves onto the queue lands on
  `tai42_backend_rq.queue.TaskBudgetQueue`, so it is stored with
  `RQ_TASK_TIMEOUT`; the worker runs every job under its own current
  `RQ_TASK_TIMEOUT`.
- `dashboard [rq-dashboard options]` — runs the RQ web dashboard. It opens its
  own Redis connection from its `--redis-url` option, outside the backend's
  connection settings, so the `RQ_` retry and timeouts do not apply to it.

## Liveness

The `worker` and `beat` processes report each pass of their loop into a liveness
file at `TAI_BACKEND_LIVENESS_PATH`.
`tai42-backend-probe` (shipped with `tai42-kit`) run in a process's
environment exits 0 while the promise its liveness file holds is not overdue by
more than `TAI_BACKEND_LIVENESS_GRACE` seconds, and 1 otherwise, so it serves as
that container's exec liveness probe. Each process on one filesystem needs its
own `TAI_BACKEND_LIVENESS_PATH`, and removes its file when it exits.

- `worker` reports on each RQ heartbeat: every dequeue poll (at most 5s apart),
  every job boundary, and once a second while `rq suspend` holds it. A job that
  arrives while a config reload re-imports the manifest modules waits for the
  re-import to finish, and the worker reports once a second while it waits.
  While a forked work-horse runs a job, the worker's horse monitor reports; on
  the `solo` and `gevent` pools, where the job runs in the worker process, a
  monitor thread reports once a second until the job ends. It is an OS thread
  under `gevent` too, so a job that holds the CPU does not starve it.
  `solo` ends a job at `RQ_TASK_TIMEOUT` only while it runs Python code, not
  while it blocks in a C call; `gevent` ends it only when it yields to gevent,
  not while it holds the CPU or blocks without yielding; and on either pool a
  job that catches the timeout runs on. So the monitor stops reporting once a
  job is still running a second past its timeout, and the probe ends the worker.
  A report that fails (the file is owned by another live process, or cannot be
  written) stops the worker before its next job, and the launch exits non-zero.
- `beat` reports each pass of the scheduler loop, promising the next within its
  `-i/--interval`.
- `dashboard` serves HTTP, so it is probed over HTTP instead: `GET /favicon.ico`
  answers 200 while the dashboard serves; under `--url-prefix` the path is
  `/<prefix>/favicon.ico`. With `--username` set, that path sits behind the same
  basic auth as the rest of the dashboard, so the probe sends the `Authorization`
  header.

## Fleet control

Fleet propagation of config changes (manifest updates, MCP/tool/config reloads)
is carried by the skeleton's internal worker bus, which every process — this
backend runtime included — joins through the app context's single long-lived
bus subscription; this backend ships no control plane of its own.

## Fork safety

The worker installs the kit's fork guard (an `os.register_at_fork` hook) on
*every* pool. It covers every fork that runs the interpreter's fork hooks:
`os.fork`, `multiprocessing`'s fork context, and a `subprocess` started with a
`preexec_fn`. A `subprocess` started without one runs no fork hooks and no
Python before it execs, so nothing applies to it. Every child the guard covers —
the work-horses, the `rq-scheduler` child that `prefork` and `solo` spawn, and
any such child other code in the process forks — resets the fork-gate state it
inherited and shuts down the monitoring writer it inherited (the vendor
client's background threads do not survive `fork()`), then rebuilds a clean
client lazily. On the prefork pool on macOS the worker also disables `urllib`
system proxy detection, which deadlocks in forked work-horses during SSL setup.
Every step logs loudly; telemetry is never silently disabled.

The fork gate is the mutual exclusion between this worker and a config
reload. A reload pops the manifest modules out of `sys.modules` and re-imports
them; a child forked during that window inherits a held `importlib` lock whose
owner thread no longer exists and hangs forever on its first import. So:

- **prefork / solo** hold the gate across the `fork()` instant of every child
  the process forks off an event loop's thread, through the kit's fork guard:
  each prefork work-horse and every `rq-scheduler` spawn. A child forked
  outside the window is immune however long its job runs. A job that arrives
  mid-reload waits for the re-import to finish, up to 120s, before forking, and
  reports the worker's progress each second while it waits, so the probe reads
  it alive: that wait is on the work loop's own thread and holds up its next
  pass. A fork from any other thread waits the same way and reports nothing,
  since the work loop passes or stalls without it. The gevent worker forks no
  child of its own (it runs rq's scheduler as a thread). Under its patch a
  thread started with `threading` is a green thread on the OS thread that runs
  the host's loop, and the kit's guard never makes a fork on a loop's thread
  wait, since that would block the loop. A native OS thread runs no loop (the
  monitor thread each in-process job starts, gevent's own threadpool), so a
  Python-level fork from one waits out a running re-import, reporting nothing;
  the monitor thread itself never forks. On that pool the guard otherwise
  resets the gate state and evicts the monitoring writer in the children that
  other code forks with a Python-level fork.
- **solo / gevent** run jobs in-process, with no inherited snapshot to protect
  them, so the gate is held for the whole job. A job that arrives mid-reload
  waits for the re-import the same way, reporting each second; a config reload
  arriving while one is running waits for it, up to 30s. A fork the job makes
  itself, on its own thread or in a sync tool it runs off the event loop,
  falls inside the job's span and goes ahead at once; a fork from a thread the
  job starts with `threading` takes a span of its own. With no reload pending
  it goes ahead at once; while a reload is pending it waits behind it, and
  that reload first waits out its 30s for the job's span to close and logs
  that it could not quiesce, so the fork goes ahead only after the re-import.
  A thread started in `contextvars.copy_context().run` stays inside the job's
  span. A job waiting on a config reload's lock does not hold that reload up.

Both waits are bounded and proceed loudly (ERROR) rather than deadlocking if
the budget is exhausted.

## Tools

The kit registers the `backend_*` tool surface over this backend's worker
inspection and schedule store (`tai42_kit.backend.register_inspection_tools`
and `register_schedule_tools`): each tool takes the same arguments and gives
the same reply shape on every backend, and a tool or form of it the backend
cannot serve raises `tai42_kit.backend.UnsupportedBackendTool` (a
`NotImplementedError`).

Task/worker tools: `backend_task_status`, `backend_task_result` (raises the
task's stored failure — the persisted traceback text — when the task FAILED; an
unfinished task replies `Task <id> is not ready (status: <status>)`),
`backend_cancel_task` (RQ's own `Job.cancel` for a job no worker has claimed; a
recurring job is first taken off the scheduler. `terminate=True` also stops a
running job with RQ's stop-job command; without `terminate` a job that has not
started is kept from running, and a job a worker has claimed or is running is
left to finish, its record untouched. A job that ends on the stop is marked
`stopped`. On the `prefork` pool the stop kills the work-horse with `SIGKILL`,
so the job's cleanup does not run. On the `solo` and `gevent` pools, which run
the job in the worker process, it is raised into the job the way the pool's
task timeout is — into the job's OS thread on `solo`, into its greenlet on
`gevent` — and the job's async work is cancelled and run to its end, its
cleanup included; a job blocked in C code, or holding the CPU on `gevent`, is
ended when that returns. Async work that catches the cancel and runs on keeps
the job running: the job still ends on the stop, marked `stopped`, if that work
returns within `RQ_TASK_TIMEOUT`; once it runs a second past it, the worker
stops reporting its liveness and the probe ends the worker. The task timeout and
the stop are raised into a job once between them, whichever comes first, and the
other is not raised: a stop sent once the task timeout has fired raises nothing
into the job, which runs on until the probe ends the worker a second past that
timeout, and is still marked `stopped` if it ends in rq's failure handling
before then. A job that ends before the stop lands is reported with the status
it reached, and a stop not confirmed within the task timeout says so),
`backend_active_tasks` (keyed by worker name; each running job carries its `id`,
`description`, `origin` queue, `status`, `enqueued_at` and `started_at` from
RQ's job record),
`backend_reserved_tasks` (keyed by queue name — RQ reserves work per queue),
`backend_scheduled_tasks` (keyed by job id, value carries `next_run_at_ts`),
`backend_worker_stats` (keyed by worker name), `backend_worker_queues`,
`backend_ping_worker`, `backend_list_active_workers`. `backend_ping_worker`,
`backend_active_tasks`, `backend_worker_stats` and `backend_worker_queues`
take a `worker_name` that narrows the call to one worker.

Schedule tools: `backend_schedule_exists`, `backend_get_schedule` (the
contract's `ScheduleDetail`: the listing row plus `args` and `kwargs`),
`backend_list_schedules` (the contract's `ScheduleListRow`: `name` / `tool` /
`cadence` / `enabled` / `next_run_at_ts` / `next_run_at_iso` / `error`;
`enabled` is always true because RQ has no disabled-schedule state),
`backend_delete_schedule`, `backend_enable_schedule` (writes nothing: every
stored schedule is enabled; replies `enabled` or `not_found`),
`backend_disable_schedule` (raises `UnsupportedBackendTool` and leaves the
schedule as it is — RQ has no disabled-schedule state),
`backend_run_schedule_now`, `backend_update_schedule`,
`backend_export_schedules`, `backend_import_schedules` (the contract's
`ScheduleRecord` rows and `ScheduleImportReport`; per-row errors surfaced as
`{"index", "name", "error"}`, never swallowed; a relative interval or a
disabled row is a per-row error). A status reply is `{"status", "name"}`; an
update given no change replies `skipped` with `No changes provided; nothing
updated.`

Not supported on RQ (raise `UnsupportedBackendTool`): `backend_registered_tasks`,
`backend_list_failed_tasks`.

## Configuration

Env group `RQ_` (a `tai42_kit` settings class, cached and reset on live reload).
`RQ_REDIS_URL` and `RQ_REDIS_MAX_CONNECTIONS` fall back to the shared
`TAI_DEFAULT_REDIS_URL` / `TAI_DEFAULT_REDIS_MAX_CONNECTIONS` when unset; the
retry and socket settings are read from `RQ_` alone. Every Redis connection of
the backend — the tools, the enqueue path, the worker and the beat — is built
from them.

| Env var | Default | Meaning |
| --- | --- | --- |
| `RQ_REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL (broker and results). |
| `RQ_REDIS_MAX_CONNECTIONS` | — | Optional cap on the pool's connections. |
| `RQ_RETRY_ATTEMPTS` | `0` | Times a failed Redis connect or command is retried, each retry logged as a warning; `0` makes one attempt. A refused password is never retried by this setting; a waiting worker's own RQ reconnect loop and its pub/sub thread still retry it. |
| `RQ_RETRY_ON_TIMEOUT` | `false` | With `RQ_RETRY_ATTEMPTS` at `0`, retry a timed-out command once at once. |
| `RQ_SOCKET_TIMEOUT` | — | Seconds a Redis command may wait on its socket; redis-py's default when unset. |
| `RQ_SOCKET_CONNECT_TIMEOUT` | — | Seconds a Redis connect may take; redis-py's default when unset. |
| `RQ_RQ_PREFIX` | `rq:` | Key prefix RQ uses for its Redis structures. |
| `RQ_QUEUE_NAME` | `default` | The RQ queue the host enqueues to, `beat` schedules on, and a worker started without `--queue` serves; a `--queue <label>` worker serves `<RQ_QUEUE_NAME>.<label>`. Every process of one stack sets the same value. Stacks sharing one Redis DB set distinct values, and no stack's value is another stack's value followed by `.<label>`. |
| `RQ_MANIFEST_KEY` | `MANIFEST_KEY` | Env key the worker CLI stores the manifest JSON under (inherited by forked work-horses). |
| `RQ_TASK_TIMEOUT` | `300` | The dispatch job budget in seconds: the timeout the worker runs every job under (enqueued or scheduler-fired, whatever timeout the job was stored with), and how long a `sync_task` dispatch waits for its job's result. |
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
