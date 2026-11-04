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

- Backend & fleet-control concept: https://docs.tai42.ai/concepts/backends
- Writing a backend (section of the backend concept): https://docs.tai42.ai/concepts/backends#writing-a-backend
- Plugin catalog: https://docs.tai42.ai/plugins

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

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

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

A config that registers a task backend also needs the worker bus and a shared
hooks registry: `tai serve`, `tai backend`, a config reload and a config write all
refuse it unless `TAI_BUS_REDIS_URL` and `HOOKS_REDIS_URL` are set (each falls back
to `TAI_DEFAULT_REDIS_URL`). The backend process matches the events it raises
against its own hooks registry, so an in-memory one would run none of the hooks
registered through the server.

The worker runtime is started through the host's backend launcher with one of:

```bash
tai backend worker [--queue LABEL] [celery worker options...]
tai backend beat
tai backend flower
```

A worker consumes one queue: `CELERY_QUEUE_NAME`, or with `--queue <label>` the
queue `<CELERY_QUEUE_NAME>.<label>` alone (one label, letters, digits, `_` or
`-`; any other value is refused before the worker starts). Celery's own
`-Q`/`--queues` option, and `CELERY_WORKER_QUEUES` in the environment, are
refused: the queue a worker consumes comes from its label.

### Green pools

```bash
tai backend worker -P gevent
tai backend worker -P eventlet
```

On `-P gevent` or `-P eventlet` the worker must be patched by Celery's own
patch (`celery.maybe_patch_concurrency`) before anything else is imported. The
worker declares that patch as its process prelude, so `tai backend worker` reads
its configuration, then restarts itself once, in place, with the patch applied
first: the same process id and the same arguments, and one
`restarting with process prelude celery:maybe_patch_concurrency` log line.

- The patch reads the pool only from the first `-P <pool>`, `--pool <pool>` or
  `--pool=<pool>` argument. A green pool given any other way (`-Pgevent`,
  combined short flags such as `-EPgevent`, `CELERY_WORKER_POOL`) or two different pools
  given where one is green is refused at launch, naming those spellings.
- Celery refuses `-B` on a green pool; run the beat as `tai backend beat`.
- Every job's coroutine runs on the host's event loop, the one loop the
  process's OS thread runs, beside the process's own work on it (the worker
  bus subscription). The kit resources bound to that loop (pooled clients,
  checkpoint savers, stores) are the host's, released at its stop.

## Fleet ops and the worker bus

Fleet config propagation lives in the skeleton's worker bus, internal to the
app: a backend-runtime process joins that bus through the app's single
long-lived subscription and receives each op (`reload_config`, `reload_mcp`, …)
exactly like a serving HTTP worker — this backend carries no control-plane of
its own. Its one obligation on top of the bus is the prefork-pool turnover:
after every applied op that declares an effect (the tool registry or the
compiled-template cache changed) the backend re-forks this worker's pool (see
[Live reload and the prefork pool](#live-reload-and-the-prefork-pool)).

The worker runs on a background thread so this process's event loop stays alive
to keep reading the bus subscription. The host owns the process's signals on
every pool: `launch` routes SIGTERM/SIGINT to a warm (then cold) Celery
shutdown, and the app's worker application installs no signal handler of its
own (neither Celery's stop handlers nor its SIGHUP restart, its SIGUSR1 stack
dump or its SIGUSR2 remote debugger).

## Liveness

The `worker` and `beat` processes report the passes of their loop into a
liveness file at `TAI_BACKEND_LIVENESS_PATH`. `tai42-backend-probe` (shipped
with `tai42-kit`) run in a process's environment exits 0 while the promise its
file holds is not overdue by more than `TAI_BACKEND_LIVENESS_GRACE` seconds, and
1 otherwise, so it serves as that container's exec liveness probe. Each process
on one filesystem needs its own `TAI_BACKEND_LIVENESS_PATH`, and removes its
file when it exits.

- `worker` reports once a second from a consumer bootstep on the timer of
  Celery's event loop, so a report proves the loop is passing. While the broker
  connection is down the loop does not pass, so the file goes stale; the
  reports resume once the consumer reconnects. Per pool:
  - `prefork`: the jobs run in the pool children and the loop goes on while
    they run; a child stuck in a job is the pool's to end, by
    `CELERY_TASK_TIMEOUT`.
  - `solo`: the job runs on the loop's own thread, so the running job reports
    once a second from its own asyncio loop. A job that blocks that loop stops
    the reports. The pool cannot end a job, so a job past
    `CELERY_TASK_TIMEOUT` holds a wedged slot: the worker then reports
    nothing, so the probe fails and the orchestrator restarts it.
  - `threads`: the loop goes on while the jobs run in the pool's threads; a
    job past `CELERY_TASK_TIMEOUT` holds its thread for good, so once one such
    job runs the worker reports nothing and the probe ends it (the healthy jobs
    beside it end with the worker and are not retried, as celery acknowledges a
    task when it picks it up; a thread cannot be ended on its own).
  - `gevent`, `eventlet`: the timer runs on its own green thread, so there a
    report proves the process and that thread. On these pools every job of a
    worker runs on the host's event loop, so a blocking step in one job's
    coroutine stalls the async work of the others and the host's own. The `gevent` pool ends a job's
    wait at `CELERY_TASK_TIMEOUT` and cancels its async work; the job holds its
    slot until that work has ended. On `gevent`, as on `solo`, `threads` and
    `eventlet`, once a job still runs a tick past the budget the worker reports
    nothing and the probe ends it: on `gevent` because the cancel did not end
    the work, on the others because they cannot end a job at all.
- `worker -B` embeds a beat, which reports each tick into its own file,
  `<TAI_BACKEND_LIVENESS_PATH>-beat`, with its own pid. The worker reports
  nothing while its beat's promise is overdue, so the one probe on the
  worker's file covers both. When the worker exits, even before it ever
  reached its broker, it ends its beat and removes both files.
- `beat` reports each tick of RedBeat's scheduler, promising the next within
  the interval the beat sleeps (at most `--max-interval`). A beat that cannot
  hold RedBeat's lock waits for it before its first tick, trying once a second
  and reporting each try, so it reads alive while it waits and starts ticking
  within a second of the lock lapsing (RedBeat holds it for 5 × `--max-interval`,
  300 s at the default, and a cleanly stopped beat releases it at once). Run one
  beat (standalone or embedded with `-B`); a second one waits and takes over
  only if the first stops. The app
  names the reporting scheduler as its `beat_scheduler`. `tai backend worker`
  and `tai backend beat` refuse any other scheduler at launch: `--scheduler`
  (`-S`), the `CELERY_WORKER_SCHEDULER` / `CELERY_BEAT_SCHEDULER` environment
  variables, and `beat_scheduler=` as command-line configuration.
- `flower` serves HTTP, so it is probed over HTTP instead: `GET /healthcheck`
  answers 200 while Flower serves, without authentication even when Flower's
  own auth is on. Under `--url_prefix`, the path is `/<prefix>/healthcheck`.

## Task execution

The `sync_task` / `async_task` extension branches dispatch the wrapped tool to
the queue via the `celery.tool_execution` task. A dispatched tool runs once;
retries are declared per tool with a `ToolRetryPolicy` and applied by the
platform, never by Celery (Celery still retries publishing the task message on
connection loss, the `retry` option below). Every task — dispatched or fired by
beat — is bounded by the dispatch job budget `CELERY_TASK_TIMEOUT` (Celery's
`task_time_limit`: the prefork pool enforces it by ending the child, the `gevent`
pool cancels the job's work at it, and on every in-process pool a job still
running a tick past the budget stops the worker's liveness reports, see
[Liveness](#liveness)). Task options exposed on every branch: `queue` (a worker
queue label; see below), `countdown`, `priority`, `retry`, `routing_key`,
`expires`, `eta`, and `callback_kwargs` — a callback schema chained via a
Celery `link` that runs `celery.callback_task` on the result (jq condition
gate, jq expression transform, optional follow-up tool). The callback runs only
when the task succeeds; a failed task chains none.

`queue` sends the task, and its callback, to the worker started with `--queue <label>`: a label of
letters, digits, `_` and `-` derives the queue `<CELERY_QUEUE_NAME>.<label>`,
and any other value raises `ValueError` before anything is sent. Unset, the
task goes to `CELERY_QUEUE_NAME`. A task sent to a queue no worker consumes
waits: a `sync_task` raises `TimeoutError` after `CELERY_TASK_TIMEOUT`, and an
`async_task` id stays pending. A tool with its own `queue` parameter cannot
take a `sync_task` or `async_task` branch. Schedules carry no queue: beat sends
every fire to `CELERY_QUEUE_NAME`, so recurring schedules need a worker without
a label.

A job does not wait for another job: it chains the next one with
`callback_kwargs`. `sync_task` and `backend_task_result` (given a `timeout`) are
for callers outside a job and raise `RuntimeError` inside one, before anything
is dispatched; `backend_task_result` with no `timeout` reads the current state and
works anywhere.

`schedule_task` persists a RedBeat entry from the contract's typed cadence
(`tai42_contract.backend.scheduling`: an interval with `kind: "interval"`,
`every` seconds and `relative`, which this backend honours, or a five-field
crontab with `kind: "crontab"`). The backend maps the cadence to RedBeat's own
stored shape, so RedBeat's entries are unchanged. Beat must run with the RedBeat
scheduler, which `launch("beat")` configures automatically.

Worker fork safety: every child the worker process forks (the pool children and
the `-B` beat child among them) evicts the monitoring vendor client through the
contract's fork-safe `writer.shutdown()` (logged, never a silent telemetry
disable), from the kit's fork guard. On macOS each freshly forked pool child
logs a warning naming the platform's fork/DNS hazard; prefer the `solo` or
`threads` pool there.

Worker shutdown: the process that ran the jobs shuts their work down once,
whenever Celery runs its warm stop: on `prefork` in each child as it exits, and
on `solo`, `threads`, `gevent` and `eventlet` in the worker process once the
pool has stopped (`shut_down_jobs`). The step closes the jobs' own task loops
with the kit resources bound to them (pooled clients, checkpoint savers, stores)
and lets the host's loop go (a later job on a green pool is refused; the loop
and its resources are the host's, released at its stop), then flushes buffered
spans. A stop Celery takes as a cold stop, or a child
killed at the hard time limit, may cut the step short or skip it.

## Tools

The kit registers the `backend_*` tool surface over this backend's worker
inspection and schedule store (`tai42_kit.backend.register_inspection_tools`
and `register_schedule_tools`), as tools of `tai42_backend_celery`: each tool
takes the same arguments and gives the same reply shape on every backend, and a
tool or form of it the backend cannot serve raises
`tai42_kit.backend.UnsupportedBackendTool` (a `NotImplementedError`).

Task/worker tools: `backend_ping_worker`, `backend_list_active_workers`,
`backend_task_status` (the result backend's state, `PENDING` for a task it does
not know), `backend_task_result` (timeout-aware; re-raises task exceptions; an
unfinished task replies `Task <id> is not ready (status: <state>)`),
`backend_cancel_task` (a revoke, awaited for the workers' replies within the
fleet-view timeout; `terminate=True` also ends the running task where its
worker's pool can: the `prefork` pool signals the task's child process, the
`gevent` and `eventlet` pools kill the task's greenlet, which the job's wait
turns into the cancel of its async work; the `threads` pool cannot end a thread,
so its worker refuses the stop and the tool raises that refusal, the task
revoked and running to its end; a `solo` worker reads no control message while
it runs a job, so a stop reaches it only once the job has ended, and the reply
says the stop is not confirmed; the workers' answers come only from the workers
that heard the revoke, so a stop is confirmed only by the worker that ended the
task; a task that has already ended gets no revoke and is reported with its
state; the state a reply carries is the result backend's at that moment, and it
reads `REVOKED` from the moment any worker has heard the revoke, whether or not
the task has ended; the reply names what was done — `revoked`, `revoked and
stopped`, `cannot be canceled (status: …)`, `cannot be stopped (status: …)`,
`revoke requested but not confirmed (status: …)` or `stop requested but not
confirmed (status: …)`), `backend_active_tasks`,
`backend_reserved_tasks`, `backend_scheduled_tasks`, `backend_registered_tasks`,
`backend_worker_stats`, `backend_worker_queues`. The fleet-view tools return
Celery's inspect shape — a mapping of worker name to that worker's entries — and
`backend_ping_worker`, `backend_active_tasks`, `backend_registered_tasks`,
`backend_worker_stats` and `backend_worker_queues` take a `worker_name` that
narrows the call to one worker. `backend_list_failed_tasks` raises
`UnsupportedBackendTool` — Celery keeps no queryable failed-task index.

Schedule tools (RedBeat): `backend_list_schedules` (the contract's
`ScheduleListRow`: `name` / `tool` / `cadence` / `enabled` / `next_run_at_ts` /
`next_run_at_iso` / `error`, never the stored arguments), `backend_get_schedule`
(the contract's `ScheduleDetail`: the row plus `args` and `kwargs`),
`backend_schedule_exists`, `backend_enable_schedule`,
`backend_disable_schedule`, `backend_run_schedule_now`,
`backend_update_schedule`, `backend_delete_schedule`, plus the backup round
trip `backend_export_schedules` / `backend_import_schedules` (the contract's
`ScheduleRecord` rows, the fired `tool` split out of the stored kwargs; a
`ScheduleImportReport` with skip/overwrite import mode keyed by name; per-row
errors surfaced as `{"index", "name", "error"}`, never swallowed). A status
reply is `{"status", "name"}` (`not_found` when no schedule has the name); an
update given no change replies `skipped` with `No changes provided; nothing
updated.`

## Configuration

Settings are read from the `CELERY_` environment group (see `CelerySettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `CELERY_BROKER_URL` | `amqp://localhost:5672//` | Celery broker URL — task queue and Celery pool control |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery result backend URL |
| `CELERY_REDBEAT_REDIS_URL` | `redis://localhost:6379/0` | Redis URL for the RedBeat schedule store |
| `CELERY_REDBEAT_KEY_PREFIX` | `redbeat:` | RedBeat key prefix (schedule zset is `redbeat::schedule`) |
| `CELERY_REDBEAT_REDIS_MAX_CONNECTIONS` | — | Caps the RedBeat store's connection pool; uncapped when unset |
| `CELERY_REDBEAT_RETRY_ATTEMPTS` | `0` | Times a failed RedBeat Redis connect or command is retried, each retry logged as a warning; `0` makes one attempt. A refused password is raised at once, never retried |
| `CELERY_REDBEAT_RETRY_ON_TIMEOUT` | `false` | With `CELERY_REDBEAT_RETRY_ATTEMPTS` at `0`, retries a timed-out command once at once |
| `CELERY_REDBEAT_SOCKET_TIMEOUT` | — | Seconds a RedBeat command may wait on its socket; redis-py's default when unset |
| `CELERY_REDBEAT_SOCKET_CONNECT_TIMEOUT` | — | Seconds a RedBeat connect may take; redis-py's default when unset |
| `CELERY_RESULT_BACKEND_RETRY_ATTEMPTS` | `0` | A Redis result backend: times a failed connect or command is retried, each retry logged as a warning; `0` makes one attempt. A refused password is raised at once, never retried |
| `CELERY_RESULT_BACKEND_RETRY_ON_TIMEOUT` | `false` | A Redis result backend: with `…_RETRY_ATTEMPTS` at `0`, retries a timed-out command once at once |
| `CELERY_RESULT_BACKEND_SOCKET_TIMEOUT` | — | A Redis result backend: seconds a command may wait on its socket; celery's default when unset |
| `CELERY_RESULT_BACKEND_SOCKET_CONNECT_TIMEOUT` | — | A Redis result backend: seconds a connect may take; celery's default when unset |
| `CELERY_BROKER_RETRY_ATTEMPTS` | `0` | A Redis broker, read and write sides alike: times a failed connect or command is retried, each retry logged as a warning; `0` makes one attempt. Never retried: the reads kombu polls (a worker's task and fanout reads, a control command's reply read), where a drop raises. A refused password is never retried by this setting; kombu's own connect step and a running worker's reconnect loop still retry it |
| `CELERY_BROKER_RETRY_ON_TIMEOUT` | `false` | A Redis broker: with `…_RETRY_ATTEMPTS` at `0`, retries a timed-out command once at once |
| `CELERY_BROKER_SOCKET_TIMEOUT` | — | A Redis broker: seconds a command may wait on its socket; kombu's default when unset |
| `CELERY_BROKER_SOCKET_CONNECT_TIMEOUT` | — | A Redis broker: seconds a connect may take; kombu's default when unset |
| `CELERY_BEAT_MAX_LOOP_INTERVAL` | `60` | Maximum seconds between beat scheduler ticks |
| `CELERY_QUEUE_NAME` | `celery` | The Celery queue the host sends to, beat fires on, and a worker started without `--queue` consumes; a `--queue <label>` worker consumes `<CELERY_QUEUE_NAME>.<label>`. Every process of one stack sets the same value. Stacks sharing one broker set distinct values, and no stack's value is another stack's value followed by `.<label>`. |
| `CELERY_TOOL_NAME_ARG` | `backend_tool_name` | Kwarg name the dispatch injects the target tool into |
| `CELERY_TASK_TIMEOUT` | `300` | The dispatch job budget in seconds: the longest a task may run on the worker (`task_time_limit`), and how long a `sync_task` branch waits for the result |
| `CELERY_MANIFEST_KEY` | `MANIFEST_KEY` | Env var carrying the live manifest JSON (prefork inheritance) |
| `CELERY_WORKER_CONCURRENCY` | `1` | Prefork pool size — one child by default |

`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `CELERY_REDBEAT_REDIS_URL` and
`CELERY_REDBEAT_REDIS_MAX_CONNECTIONS` fall back to the shared
`TAI_DEFAULT_REDIS_URL` / `TAI_DEFAULT_REDIS_MAX_CONNECTIONS` when unset; the
retry and socket settings are read from their own namespace alone.

### Redis connections

A Redis result backend takes its retry and timeouts from
`CELERY_RESULT_BACKEND_RETRY_ATTEMPTS`, `…_RETRY_ON_TIMEOUT`,
`…_SOCKET_TIMEOUT` and `…_SOCKET_CONNECT_TIMEOUT`; a Redis broker takes the
same four under `CELERY_BROKER_`, for the read and the write side alike; the
RedBeat store takes them under `CELERY_REDBEAT_`. A knob left unset keeps
celery's or kombu's own default. Every retry is logged as a warning, and the
last failure is raised.

- The plugin picks the Redis result backend and broker transport classes
  itself, so a URL from `CELERY_RESULT_BACKEND`, `CELERY_BROKER_URL`,
  `CELERY_BROKER_READ_URL` or `CELERY_BROKER_WRITE_URL` goes through them
  alike. `CELERY_LOADER` and `--loader` do not change the app's loader.
- The Redis URL forms taken are `redis://`, `rediss://` and `redis+socket://`
  (and `redis://`, `rediss://` and `unix://` for `CELERY_REDBEAT_REDIS_URL`).
  Every other Redis form — `sentinel://`, a `;` failover list, `unix://` or
  `socket://` for the result backend or the broker, `redis-cluster://` or
  `redis-sentinel://` for RedBeat — stops the app at startup with an error
  naming the setting. A non-Redis broker or result backend (AMQP, `rpc://`, …)
  is left to celery, with celery's own retries.
- With `RETRY_ATTEMPTS` unset, a Redis connection makes one attempt: a result
  write fails on the first error, a worker whose Redis read side is not
  reachable at start exits, and a publish over a Redis write side fails at
  once. Celery's own startup connect retry is switched off when the read side
  is Redis, and its publish retry when the write side is Redis, each side on
  its own; the namespace's `RETRY_ATTEMPTS` takes their place.
- A running worker that loses its Redis broker keeps reconnecting through
  celery's own loop (`broker_connection_max_retries`), logged at ERROR.
- `CELERY_BROKER_RETRY_ATTEMPTS` never retries a read kombu polls: a worker's
  task and fanout reads, and a control command's reply read (`inspect()`,
  `control`) in any process. A drop there raises at once. A worker's drop
  restarts celery's consumer, which reconnects; a control call fails with the
  connection error, and a retry of that call reconnects.
- A result wait whose Redis result backend drops is reconnected and
  re-subscribed by `CELERY_RESULT_BACKEND_RETRY_ATTEMPTS`; after each reconnect
  it reads the stored state of every task it still awaits, so a result written
  meanwhile still arrives. When that retry runs out, the wait raises the
  connection error, and the next wait, on any task, connects again, re-subscribes
  every task still awaited and reads their stored state.
- kombu's own connect step retries once after 2 s, and its channel open
  retries within `broker_connection_timeout`; neither is logged.
- flower's events, inspector and results go through the plugin's app and the
  settings above. Its broker and queue-length views open their own Redis
  connection per request, outside these settings, with no retry.

### Live reload and the prefork pool

After the worker bus applies an op in this process that declares an effect (the
post-apply hook receives the op's `effects`), the backend re-forks
this worker's prefork pool so its children re-inherit the parent's updated tool
registry, and it CONFIRMS the restart rather than assuming it: it polls `stats` and
`active` until every idle pre-restart child PID is gone and the pool is back to full
size. Celery's `pool_restart` arms a per-child restart sentinel, and a child checks it
before taking each task: an idle child recycles on its ~1s poll wakeup, while a child
busy with a task finishes that task, exits after it and takes no new one, so it is not
waited for (the confirmation logs its PID at INFO). That includes a task that published
the very op being applied, which is itself waiting on this worker's reply. A pool whose
idle pre-restart children do not leave, or that does not refill, fails the op loudly,
naming the worker — turning this worker's bus reply into `failed` rather than
reporting an applied op behind stale children. A query op such as `list_failed_mcps`
declares no effect, so it never re-forks the pool.

The turnover runs inside the op apply, before this worker's terminal reply is sent, and
the publisher waits only its bus apply window (`TAI_BUS_APPLY_TIMEOUT`, default `90s`) for
that reply. The host hands the fleet-op handler what is left of that window, measured from
this worker's receipt of the op, and the confirmation budget sits a small margin under it:
the confirm or raise reaches the publisher before its report cut, so a stalled turnover is
recorded as a truthful `failed` rather than a `timed_out` guess. Raising
`TAI_BUS_APPLY_TIMEOUT` raises the turnover budget under it.

A worker that leaves (its drain is requested, or its Celery worker exits) abandons a
turnover in flight and any that starts after: its consumer stops answering the
`stats` and `active` polls, and its pool takes no new task, so no pre-restart child
serves another one. The turnover logs a WARNING naming the worker and the op and
returns once the control round in flight ends (its `stats` and `active` polls
wait at most 2 s each for a reply), so the worker exits within its drain rather
than holding the process open for the rest of the turnover budget.

### Config reloads and the fork gate

A config reload pops the manifest modules out of `sys.modules` and re-imports
them. A child forked during that window inherits a held `importlib` lock whose
owner thread no longer exists and hangs forever on its first import, and a job
running in the worker process meanwhile reads half-imported modules. The fork
gate is the mutual exclusion between this worker and a config reload:

- **every pool** holds the gate across the `fork()` instant of every child the
  worker forks — the `prefork` pool's children, the ones a pool turnover forks,
  and the beat child of `-B` — through the kit's fork guard (an
  `os.register_at_fork` hook). A fork that arrives mid-reload waits for the
  re-import to finish, up to 120s. A fork waiting on the worker's hub thread
  holds up the hub's next pass, so the worker reports its progress each second
  while it waits and the probe reads it alive; a fork waiting on any other
  thread reports nothing, since the hub passes or stalls without it.
- **solo / threads / gevent / eventlet** run jobs in the worker process, so
  each job holds the gate for its whole run. A job that arrives mid-reload
  waits for the re-import the same way, reporting each second when it waits on
  the hub's thread (`solo`) and nothing on a thread of its own; a config reload
  arriving while jobs run waits for them, up to 30s. A fork a job makes
  itself, on its own thread or in a sync tool it runs off the event loop,
  falls inside the job's span and goes ahead at once; a fork from a thread the
  job starts with `threading` takes a span of its own. With no reload pending
  it goes ahead at once; while a reload is pending it waits behind it, and
  that reload first waits out its 30s for the job's span to close and logs
  that it could not quiesce, so the fork goes ahead only after the re-import.
  A fork from a thread that runs an event loop never waits, as a wait would
  block that loop: it enters its span at once, so a pending reload waits it
  out, and one that lands while a reload already holds the gate goes ahead and
  is logged at ERROR. A thread started in `contextvars.copy_context().run`
  stays inside the job's span. A job waiting on a config reload's lock does
  not hold that reload up.

Both waits are bounded and proceed loudly (ERROR) rather than deadlocking if
the budget is exhausted.

The re-import refreshes what the package registers for the new configuration
(its tools and task branches) and nothing the running process launched: the
worker, `beat` and `flower` run on the Celery app object their launch built,
handed to Celery's CLI as the object rather than as an import path, and the
worker's liveness steps and reporters, its embedded beat's file and its host
loop live on that app, and its signal receivers are connected by the launch's
build and close over that app. Celery takes two of the package's
classes only by import path, the Redis result backend and the beat's scheduler,
so a thread or a beat that starts after a reload may resolve them from the
re-imported package; neither keeps state of its own, so that copy behaves as the
built one. A reload, including the one a worker's first connect makes before it
reads ready when it withdraws a stored key, leaves the running worker whole.

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
