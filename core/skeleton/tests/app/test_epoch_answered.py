"""When a serving generation counts as answered: no counted request, no driver and no gate waiter left.

Each case starts :meth:`~tai42_skeleton.app.epoch.Epoch.wait_answered` while one kind of work
of the generation is outstanding, turns the loop until the wait has run as far as it can, and
then ends that work: the wait returns only once the work is over.
"""

from __future__ import annotations

import asyncio

from tai42_skeleton.app.epoch import Epoch

from ._served_app import REQUEST_TIMEOUT_S

# Loop turns that let every task the case started run until it suspends on an event.
LOOP_TURNS = 20


async def _turn_loop() -> None:
    for _ in range(LOOP_TURNS):
        await asyncio.sleep(0)


async def test_wait_answered_waits_for_a_counted_request_to_be_released() -> None:
    epoch = Epoch(number=1)
    epoch.admit()
    waiter = asyncio.create_task(epoch.wait_answered())
    await _turn_loop()
    assert not waiter.done()

    epoch.release()
    await asyncio.wait_for(waiter, REQUEST_TIMEOUT_S)
    assert epoch.answered


async def test_wait_answered_waits_for_a_driver_of_a_gated_body_to_finish() -> None:
    epoch = Epoch(number=1)
    finish = asyncio.Event()
    driver = asyncio.create_task(finish.wait())
    epoch.driving_tasks.add(driver)
    waiter = asyncio.create_task(epoch.wait_answered())
    await _turn_loop()
    assert not waiter.done()

    finish.set()
    await asyncio.wait_for(waiter, REQUEST_TIMEOUT_S)
    assert driver.done()
    assert epoch.answered


async def test_wait_answered_waits_for_work_waiting_on_a_recorded_lock_to_stop_waiting() -> None:
    epoch = Epoch(number=1)
    stop = asyncio.Event()
    queued = asyncio.create_task(stop.wait())
    epoch.begin_task_wait(queued)
    waiter = asyncio.create_task(epoch.wait_answered())
    await _turn_loop()
    assert not waiter.done()

    epoch.end_task_wait(queued)
    await asyncio.wait_for(waiter, REQUEST_TIMEOUT_S)
    assert epoch.answered
    stop.set()
    await queued
