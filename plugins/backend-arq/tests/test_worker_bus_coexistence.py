"""Live verification: the app-owned worker-bus subscription coexists with arq
job execution on one event loop.

arq's ``launch`` (``start_arq_worker``) runs the worker on this process's asyncio
loop — the same loop the skeleton's one long-lived worker-bus subscription reads
on. This backend adds no control-plane surface of its own, so the only
reconciliation concern is that a job monopolizing the loop must not permanently
starve the subscription's pub/sub read (fleet ops must still land).

An execution backend never imports ``tai42_skeleton``, so the app-owned
subscription is modeled here over this repo's own pub/sub broker fixture
(``FakePubSubRedis``): a subscription consumer and an arq-style job run as peers
on one loop. The two observables that bound the reconciliation:

* a CPU-bound job that never yields serializes a ``reload_config`` published
  mid-job behind it — the op applies once the job completes, and is never
  dropped; and
* a job that awaits (the normal I/O-bound case) yields the loop, so the same op
  applies while the job is still in flight.
"""

from __future__ import annotations

import asyncio
import json
import time

from tests.conftest import FakePubSubRedis

_BUS_CHANNEL = "tai:bus"


async def test_cpu_bound_job_serializes_bus_op_on_shared_loop(hub: FakePubSubRedis) -> None:
    order: list[str] = []
    applied: list[str] = []
    subscribed = asyncio.Event()

    async def subscription_consumer() -> None:
        pubsub = hub.pubsub()
        await pubsub.subscribe(_BUS_CHANNEL)
        subscribed.set()
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
        assert msg is not None
        op = json.loads(msg["data"])["op"]
        applied.append(op)
        order.append(f"applied:{op}")
        await pubsub.aclose()

    consumer = asyncio.create_task(subscription_consumer())
    await subscribed.wait()

    # Broadcast the op, THEN run a job that holds the one loop with a non-yielding
    # (CPU-bound) stretch: the consumer cannot read the queued op until the job
    # returns, so its application is serialized behind the running job.
    await hub.publish(_BUS_CHANNEL, json.dumps({"op": "reload_config"}))
    order.append("job:start")
    deadline = time.perf_counter() + 0.2
    while time.perf_counter() < deadline:
        pass
    order.append("job:done")

    await asyncio.wait_for(consumer, timeout=5.0)

    # Applied exactly once, only after the job completed: shared-loop serialization
    # holds and the op is not lost.
    assert applied == ["reload_config"]
    assert order == ["job:start", "job:done", "applied:reload_config"]


async def test_awaiting_job_lets_bus_op_apply_in_flight(hub: FakePubSubRedis) -> None:
    order: list[str] = []
    subscribed = asyncio.Event()
    op_applied = asyncio.Event()

    async def subscription_consumer() -> None:
        pubsub = hub.pubsub()
        await pubsub.subscribe(_BUS_CHANNEL)
        subscribed.set()
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
        assert msg is not None
        order.append("applied:" + json.loads(msg["data"])["op"])
        op_applied.set()
        await pubsub.aclose()

    async def arq_job() -> None:
        order.append("job:start")
        # An I/O-bound job awaits, yielding the shared loop; the subscription runs
        # and applies the op before the job resumes.
        await op_applied.wait()
        order.append("job:done")

    consumer = asyncio.create_task(subscription_consumer())
    job = asyncio.create_task(arq_job())
    await subscribed.wait()
    # Let the job reach its await so the op truly lands mid-job.
    await asyncio.sleep(0)

    await hub.publish(_BUS_CHANNEL, json.dumps({"op": "reload_config"}))
    await asyncio.gather(consumer, job)

    assert order == ["job:start", "applied:reload_config", "job:done"]
