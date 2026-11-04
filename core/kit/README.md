# tai42-kit

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Generic leaf helpers, settings primitives, pooled clients, and LLM/AI factories
for the TAI ecosystem. It provides the reusable building blocks the server is
composed from: data and text transforms, LangChain/FastMCP/MCP tool glue, the
pooled-client facade and concrete drivers (`redis`, `curl`, `mcp`, `postgres`,
`http`), the SSRF URL guard and safe download, MCP client transports over a UDS
socket, the settings machinery, LLM/embedding factories with checkpoint/store
backends, logging setup, and the backend liveness file with its
`tai42-backend-probe` console script. Heavier backends are gated behind extras and
imported lazily. Typed package (`py.typed`).

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

`tai42-kit` obeys the leaf rule: its only tai-* dependency is `tai42-contract`. It
implements the contract's `BaseClient` Protocol and consumes its manifest types;
among tai-* packages it depends on nothing else.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-kit
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/core/kit
```

Backends are gated behind extras, so install the ones you need — e.g. the
pooled-client drivers `tai42-kit[redis]`, `tai42-kit[postgres]`, `tai42-kit[curl]`, the
checkpoint/store backends `tai42-kit[langgraph-checkpoint-postgres]`,
`tai42-kit[langgraph-checkpoint-sqlite]`, and LLM-provider backends like
`tai42-kit[anthropic]`, `tai42-kit[google]`, `tai42-kit[mistral]`, `tai42-kit[xai]`,
`tai42-kit[ollama]`, `tai42-kit[huggingface]`.

## Loop-bound resources

The kit keeps its pooled clients and its long-lived async resources (checkpoint
savers, stores) per event loop and per client epoch. `tai42_kit.loop_bound` holds
the registry machinery behind the resources (`ResourceRegistry`,
`LoopRegistryMap`) and the two closes. `release_loop_bound_resources()` is
awaited on the loop by whatever ends the kit's use of it: a loop's end, or an
application's shutdown on a loop that lives on. It closes every epoch's registries
for the loop, then its pooled clients, running each step even when the other fails
and raising every failure. On a loop that lives on, the next lease builds its pool
or registry anew and the next release closes it. `PrivateLoop.close()` marks its
loop closing before it releases (`tai42_kit.loop_closing`): from then on a new
lease of a pooled client, a registry or a registry's resource on that loop raises
a `RuntimeError` naming the loop as closing, as a closed connection pool refuses a
new connection, so nothing is opened that no release would close. A lease held
from before keeps working until it is released, and every other loop leases as
before. A loop that is not marked (one `asyncio.run` ends) refuses nothing: a lease
made on it after its last release opens a resource that stays open, so the code
that ends such a loop leases nothing after that release.
`close_retired_registries(epoch)` closes one retired epoch's registries on the
running loop (the current epoch is refused); the retire of a serving generation
calls it once that generation's work has drained or its drain budget has run out.

A backend whose pool gives each running job an OS thread with no loop running on
it runs the job's coroutine on `tai42_kit.backend.PrivateLoop`, a loop the job's
thread runs itself, one job at a time; the backend makes one per job or keeps one
for job after job. While a job runs, the loop is its thread's current event loop,
and the thread has none after, as with `asyncio.Runner`. An exception raised into
the loop by none of its tasks (a pool's time limit from a signal handler) cancels
the job's task and runs it to its end, its cleanup included, before the job raises
it. The loop's `close()` first marks the loop closing, then releases the
loop-bound resources on it, while the tasks left on it are still alive (a pool whose close awaits its own workers finds
them running), then cancels the tasks on it and runs them to their end, round
after round until none is left (a task one's cleanup starts, one the release left,
work a thread hands the loop), closes the async generators left on it, ends the
tasks their close made, and closes it. A task that leases a pooled client or a
registry once the close has begun gets the closing error, and the close logs it
at ERROR when the task ends with it, as it logs every error a task ends with then.
Its default executor is shut down without waiting for its threads, so a job that
awaited a thread is not held by it once the exception that ends the job has
reached the loop; the thread ends on its own. When that exception reaches a loop
waiting for a thread is the pool's to say: a signal handler's interrupts the wait
at once, while one raised into the job's thread from another thread lands only
once the loop runs Python code again.

A backend whose pool runs its jobs as green threads of the host's OS thread
cannot give a job a loop of its own, since asyncio runs one loop per OS thread:
`tai42_kit.backend.SharedLoop` hands each job's coroutine to the host's running
loop and waits for its result, and the resources bound to that loop stay the
host's, released at its end.

## Backend task branches

`tai42_kit.backend.register_task_branches` registers the three BACKEND tool
extensions every backend offers: `<tool>_sync_task` queues the call and waits for
its result, `<tool>_async_task` queues it and replies with the job's id, and
`<tool>_schedule_task` writes a recurring schedule firing the tool. A backend
supplies only its seams: the contract's `TaskDoor`, which queues a job and waits on
it, and its `ScheduleWriter`, which stores a schedule and says whether it honours a
relative interval. The task options every backend takes are
`tai42_kit.backend.BACKEND_TASK_OPTS` (`countdown`, `eta`, `expires`, `queue`,
`callback_kwargs`); a door adds its own through `task_opts`. A `queue` value is a
label (`tai42_kit.backend.check_queue_label`: letters, digits, `_` and `-`), and the
backend sends the job to `derived_queue(<its queue name>, label)`, its queue name
followed by `.<label>`. A sync wait that runs out raises `TimeoutError` naming the
job, which may still be running; a failed job raises its failure as the backend
retains it.

## Backend tool surface

Every backend offers the same `backend_*` tools (`tai42_kit.backend.BACKEND_TOOLS`),
each with one argument list on every backend. The schedule tools also give one reply
shape on every backend; the task and worker views reply with what the backend reports.
`tai42_kit.backend.register_schedule_tools` registers the ten schedule tools
(`SCHEDULE_TOOLS`: list, get, update, enable, disable, run now, exists, delete,
export, import) over the backend's `ScheduleStore`, and
`register_inspection_tools` registers the twelve task and worker tools
(`INSPECTION_TOOLS`) over its worker inspection, a subclass of
`tai42_kit.backend.BackendInspection`. A backend passes its package name
(`__name__`) as `module`: the tools are registered as tools of that package,
tagged `backend`.

What a backend cannot serve is still registered and raises
`tai42_kit.backend.UnsupportedBackendTool` (a `NotImplementedError`) naming the
backend and the tool: each view `BackendInspection` keeps refuses, a
`worker_name` on a backend whose inspection does not set `addresses_workers`, a
`backend_cancel_task(terminate=True)` on one that does not set `terminates`, a
`backend_cancel_task(terminate=True)` for a task whose worker cannot stop it (the
backend raises it from the worker's own answer), a `backend_cancel_task` without `terminate` for a running task on one that cannot
cancel a running task without stopping it, and a
`backend_disable_schedule` on a store that holds no disabled schedule
(`holds_disabled`), whose `backend_enable_schedule` writes nothing. A schedule
status reply is `{"status", "name"}`. `tai42_kit.backend.check_tool_surface`
certifies a backend's registered tools against the surface.

## Backend liveness

`tai42_kit.liveness` holds the file a backend loop process reports its progress
into (`LivenessFile`), its `TAI_BACKEND_LIVENESS_` settings (`PATH`, `INTERVAL`,
`GRACE`), and the reading of it (`check_liveness`). Each report writes the
process's pid and a horizon: the seconds within which the next pass is
promised, the report's own promise plus `INTERVAL`. The file is rewritten only
when the promise already in it would expire before the next pass, so a steady
loop writes about once per `INTERVAL`. A file holding the pid of another live
process is refused: each process needs its own path. Of two processes claiming
one file at once, the second is refused. A process removes its file when it
exits (a file left by a process that no longer exists is removed too), and a
report after that removal raises instead of writing the file again.

The `tai42-backend-probe` console script reads the file at
`TAI_BACKEND_LIVENESS_PATH` and prints one line: it exits 0 while the file's age
is at most its horizon plus the grace (`--grace`, default
`TAI_BACKEND_LIVENESS_GRACE`), and 1 when the file is stale or missing, so it
serves as an orchestrator's exec liveness probe.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev,llm,jq,uvicorn,redis,curl,postgres]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright       # 0 errors; missing optional ML backends report as warnings
uv run --no-sync pytest --cov --cov-report=term-missing        # optional-extra tests skip if their extra is absent
```

See `CONTRIBUTING.md` for the rules.

## Documentation

The whole platform — concepts, guides, and the generated reference — lives in
the unified documentation site:

- Layering & the contract/kit/cli/skeleton split: https://docs.tai42.ai/concepts/layering
- Python SDK reference (this package's public API): https://docs.tai42.ai/reference/python-sdk

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
