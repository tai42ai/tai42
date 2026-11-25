"""The memory of one serve under a repeated jq load, and what it returns once idle.

The stack runs on the deployment's allocator environment
(:data:`~tai42_e2e.manifests.memory.MEMORY_ALLOCATOR_PRELOAD` with
:data:`~tai42_e2e.manifests.memory.MEMORY_ALLOCATOR_CONF`). Before the first sample the test
reads ``/proc/<serve pid>/maps`` and fails unless the allocator is mapped in the serve: a
preload the dynamic loader cannot satisfy only prints a line and runs on the default
allocator, so the run would measure the wrong thing. The suite runs where the deployment runs,
on Linux; a host without ``/proc`` or without the library fails with that message.

The load: ``MEMORY_PROGRAMS`` distinct jq programs, each run as one chained call
``e2e_document_chain`` -> jq (first value) -> ``e2e_record`` over a structured document of
at least ``MEMORY_DOCUMENT_TEXT_FLOOR`` bytes of JSON that the serve builds itself. A replay
runs every program once, ``MEMORY_BATCH`` calls at a time, through the sync run-tool door;
the load is ``MEMORY_REPLAYS`` replays. Beside the chained calls of every batch,
``MEMORY_DIRECT_PER_BATCH`` plain ``e2e_document`` calls go through the same door. The chained
variant is a coroutine tool and builds its document on the worker's event loop; the plain probe
is sync, and that door runs it on an executor thread of the uvicorn worker, so the load also
allocates documents on those threads. The path:

- ``POST /api/run-tool`` is the operation route ``run_tool`` (``tai42_skeleton/routers/tools.py:83``,
  ``RUN_TOOL_PATH`` at ``routers/paths.py:18``) over ``tai42_skeleton/operations/tools.py:237``;
- that operation starts the run with ``tai42_app.tools.run_tool(..., offload_sync=True)``
  (``operations/tools.py:289``), the facet that forwards to the tool binding
  (``tai42_skeleton/app/facets/tools.py:80``);
- the binding's ``run_tool`` dispatches through ``_dispatch_tool``
  (``tai42_skeleton/tools/binding/dispatch.py:104``), which sets
  ``offload = offload_sync and not inspect.iscoroutinefunction(resolved_fn)`` (``dispatch.py:165``),
  true for the sync probe, and builds ``_validation_wrapper(resolved_fn, offload)`` (``dispatch.py:171``);
- that wrapper calls ``asyncio.to_thread(resolved_fn, **kwargs)``
  (``tai42_skeleton/tools/binding/arguments.py:30``), the loop's default executor.

Before the load, once the size checks are done, an idle wait takes the baseline: the serve is
sampled every ``MEMORY_IDLE_INTERVAL_S`` seconds for ``MEMORY_IDLE_WAIT_S`` seconds and the
last sample is the reading. During the load the serve's process tree is sampled from one
process table read after every batch. The sample after the first replay is the warm-up: every
program compiled once in the children it reached, every cache primed, the pool idle. After
the last replay the same idle wait gives the end. Every sample is logged with the thread count
of each uvicorn worker, and each idle wait logs its series of totals.

The budget (:mod:`tai42_e2e.memory_budget`): no compute-pool child in any sample at or above
``MEMORY_CHILD_BOUND_KB``; the serve's end total at most ``MEMORY_GROWTH_FACTOR`` times its
warm-up total; of the burst's growth, what the highest total sampled after the baseline rose
above the baseline, the end still holds less than ``MEMORY_IDLE_RETURN_FRACTION`` (a load that
never rose above the baseline fails); at least one compute-pool child replaced
after the warm-up; and every program recorded once per replay and every plain call answered
with the whole document, so the load cannot pass by doing less work.

What the load is sized to exceed: a compute-pool child parses a job's whole document, so its
resident set after a job follows its largest input, not the sum of its inputs. The document
is of the 20 MiB class, where one job takes a child to several hundred MB; a child whose
allocator keeps that memory once the job is done sits above the child bound after its first
job, long before a replacement after ``MEMORY_WORKER_MAX_TASKS`` (128) pool jobs could clear it.

What the load is sized to reach: a program the child has not compiled costs it two pool jobs,
a program it has compiled one, so 512 chained calls plus one compile job per program per child come
to about 768 pool jobs over four children, about 192 per child, and every child crosses 128
jobs once inside the load; the first replay is about 32 jobs per child, so no child is
replaced before the warm-up sample and that sample is the baseline for turnover.

Two time bounds hold: the test's timeout marker bounds the load, and a load that does not
finish every replay fails on it; ``MEMORY_IDLE_WAIT_S`` bounds each idle wait. On a CI runner a
replay takes about 95 s, so the whole test takes about 14 minutes; the marker allows 20."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests.memory import MEMORY_ALLOCATOR_PRELOAD
from tai42_e2e.memory_budget import (
    MEMORY_CHILD_BOUND_KB,
    MEMORY_GROWTH_FACTOR,
    MEMORY_IDLE_RETURN_FRACTION,
    check_child_bound,
    check_growth,
    check_idle_return,
    check_turnover,
)
from tai42_e2e.rss import ServeMemory, serve_memory
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "probe-tools",
        "process",
        "store:redis",
        "topology:workers=2",
        "setting:extension:chain",
        "setting:compute:worker_max_tasks",
    ),
]

logger = logging.getLogger(__name__)

# Distinct jq programs, each its own cache entry in a compute-pool child.
MEMORY_PROGRAMS = 64
# How many times the load runs every program.
MEMORY_REPLAYS = 8
# Calls in flight at once; the process tree is sampled after each batch.
MEMORY_BATCH = 8
# Plain ``e2e_document`` calls beside each batch's chained calls, run on executor threads.
MEMORY_DIRECT_PER_BATCH = 2
# The JSON text of the document every call runs its program over is at least this many bytes.
MEMORY_DOCUMENT_TEXT_FLOOR = 20 * 1024 * 1024
# The document's entries; sized so its JSON text meets ``MEMORY_DOCUMENT_TEXT_FLOOR``.
MEMORY_DOCUMENT_ITEMS = 64_000
# An idle wait samples the serve every ``MEMORY_IDLE_INTERVAL_S`` seconds for
# ``MEMORY_IDLE_WAIT_S`` seconds and its last sample is the idle reading: jemalloc's
# background thread purges in steps with flat stretches of several seconds between them, so
# no short run of equal readings marks the end of the decay.
MEMORY_IDLE_INTERVAL_S = 1.0
MEMORY_IDLE_WAIT_S = 30.0


async def _run_tool(api: ApiClient, tool_name: str, arguments: dict[str, Any]) -> Any:
    """The value ``tool_name`` returned through the sync run-tool door."""
    outcome = await api.post("/api/run-tool", json={"tool_name": tool_name, "arguments": arguments})
    assert outcome["kind"] == "result", f"{tool_name} did not answer with a result: {outcome}"
    return outcome["result"]


async def _chain(api: ApiClient, program: str) -> None:
    """One chained call: the document, then ``program`` over it, then ``e2e_record`` with the
    object the program emits."""
    await _run_tool(
        api,
        "e2e_document_chain",
        {
            "items": MEMORY_DOCUMENT_ITEMS,
            "jq_expression": {"content": program},
            "next_tool_name": "e2e_record",
        },
    )


async def _direct(api: ApiClient) -> None:
    """One plain call of the sync document probe; only its entry count is kept."""
    document = await _run_tool(api, "e2e_document", {"items": MEMORY_DOCUMENT_ITEMS})
    assert len(document["items"]) == MEMORY_DOCUMENT_ITEMS, (
        f"e2e_document answered {len(document['items'])} entries, not {MEMORY_DOCUMENT_ITEMS}"
    )


def _record_values(stack: TaiStack, key: str) -> list[str]:
    return [json.loads(record)["value"] for record in stack.records(key)]


def _assert_allocator_mapped(pid: int) -> None:
    """The serve process ``pid`` has the deployment's allocator mapped."""
    maps = Path(f"/proc/{pid}/maps")
    try:
        mappings = maps.read_text()
    except OSError as exc:
        raise AssertionError(
            f"cannot read {maps} to confirm the serve runs on {MEMORY_ALLOCATOR_PRELOAD}: {exc}"
        ) from exc
    assert MEMORY_ALLOCATOR_PRELOAD in mappings, (
        f"the serve process {pid} does not map {MEMORY_ALLOCATOR_PRELOAD}: the preload was not applied "
        "(is the library installed on this host?)"
    )


def _sample(root_pid: int, label: str) -> ServeMemory:
    """Read the serve's tree and log it with each worker's thread count."""
    sample = serve_memory(root_pid)
    logger.info(
        "serve memory %s: total %d kB; worker threads %s",
        label,
        sample.total_kb,
        ", ".join(f"{worker.pid}={worker.threads}" for worker in sample.workers),
    )
    return sample


async def _idle_wait(root_pid: int, label: str) -> list[ServeMemory]:
    """Sample the serve every ``MEMORY_IDLE_INTERVAL_S`` seconds for ``MEMORY_IDLE_WAIT_S``
    seconds; the series, its last sample the idle reading."""
    series: list[ServeMemory] = []
    started = time.monotonic()

    async def waited() -> ServeMemory | None:
        series.append(_sample(root_pid, f"{label} idle {len(series) + 1}"))
        return series[-1] if time.monotonic() - started >= MEMORY_IDLE_WAIT_S else None

    # The wait ends on elapsed time; the deadline only caps a series whose samples run slow.
    await wait_for_async(
        waited,
        deadline=2 * MEMORY_IDLE_WAIT_S,
        interval=MEMORY_IDLE_INTERVAL_S,
        message=f"the {label} idle wait did not finish its samples",
    )
    logger.info("serve memory %s idle series (kB): %s", label, [taken.total_kb for taken in series])
    return series


@pytest.mark.timeout(1200)
async def test_serve_memory_stays_bounded_under_repeated_jq_load(
    memory_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = memory_stack.api()
    root_pid = memory_stack.process("serve").pid
    _assert_allocator_mapped(root_pid)

    # The load is what it claims: a document of the floor's size, reaching jq as an array.
    document = await _run_tool(api, "e2e_document", {"items": MEMORY_DOCUMENT_ITEMS})
    document_text_bytes = len(json.dumps(document, separators=(",", ":")))
    assert document_text_bytes >= MEMORY_DOCUMENT_TEXT_FLOOR, (
        f"the document renders {document_text_bytes} bytes of JSON, below the {MEMORY_DOCUMENT_TEXT_FLOOR} floor"
    )
    probe_key = uniq("mem-probe")
    await _chain(api, f'{{key: "{probe_key}", value: (.items | length | tostring)}}')
    assert _record_values(memory_stack, probe_key) == [str(MEMORY_DOCUMENT_ITEMS)], (
        f"jq did not receive the document as an array of {MEMORY_DOCUMENT_ITEMS} entries: "
        f"{memory_stack.records(probe_key)}"
    )

    baseline_series = await _idle_wait(root_pid, "baseline")
    baseline = baseline_series[-1]

    prefix = uniq("mem")
    keys = [f"{prefix}-{index}" for index in range(MEMORY_PROGRAMS)]
    programs = [f'{{key: "{key}", value: (.items[{index}].name)}}' for index, key in enumerate(keys)]
    samples: list[ServeMemory] = []
    warm: ServeMemory | None = None
    started = time.monotonic()
    for replay in range(MEMORY_REPLAYS):
        for first in range(0, MEMORY_PROGRAMS, MEMORY_BATCH):
            await asyncio.gather(
                *(_chain(api, program) for program in programs[first : first + MEMORY_BATCH]),
                *(_direct(api) for _ in range(MEMORY_DIRECT_PER_BATCH)),
            )
            samples.append(_sample(root_pid, f"replay {replay + 1} batch {first // MEMORY_BATCH + 1}"))
        if replay == 0:
            warm = samples[-1]
    elapsed = time.monotonic() - started
    assert warm is not None
    samples.extend(await _idle_wait(root_pid, "end"))
    end = samples[-1]
    peak = max(samples, key=lambda taken: taken.total_kb)
    every_sample = [*baseline_series, *samples]
    largest_child_kb = max((child.rss_kb for sample in every_sample for child in sample.children), default=0)
    logger.info(
        "serve memory: baseline %d kB, warm-up %d kB, peak %d kB, idle end %d kB, "
        "largest compute-pool child %d kB, %d calls in %.1f s",
        baseline.total_kb,
        warm.total_kb,
        peak.total_kb,
        end.total_kb,
        largest_child_kb,
        MEMORY_REPLAYS * (MEMORY_PROGRAMS + MEMORY_DIRECT_PER_BATCH * (MEMORY_PROGRAMS // MEMORY_BATCH)),
        elapsed,
    )

    check_child_bound(every_sample, bound_kb=MEMORY_CHILD_BOUND_KB)
    check_growth(warm, end, factor=MEMORY_GROWTH_FACTOR)
    check_idle_return(baseline, peak, end, fraction=MEMORY_IDLE_RETURN_FRACTION)
    check_turnover(warm, end)
    for index, key in enumerate(keys):
        expected = [f"item-{index}"] * MEMORY_REPLAYS
        assert _record_values(memory_stack, key) == expected, (
            f"program {index} did not record once per replay: {memory_stack.records(key)}"
        )
