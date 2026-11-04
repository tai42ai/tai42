"""Run a case in a fresh process shaped as ``tai backend worker`` runs a green pool, and read what it printed.

``tai backend worker -P gevent`` and ``-P eventlet`` start the process through celery's patch,
before anything else is imported; the host then serves its stdlib asyncio loop on the main green
thread, hands that loop to the jobs on the app (``AsyncTask.share_loop``) and runs the worker on a
``threading.Thread``, a green thread once patched, where every job is a green thread of the pool.
Each case here does the same in a subprocess: celery's patch first, asyncio's future chaining bound
as the host's entry binds it, then the suite's stub app (``conftest``) and the plugin; the case's
body runs on a ``threading.Thread`` while the host's loop runs on the main green thread
(``on_the_host``), starts the pool's real celery ``TaskPool``, has the progress step watch its jobs
as on a worker's consumer start (with the progress the worker's build registers on the app, the
job budget ``task_timeout`` given to ``start_pool``), and prints one JSON line. The test process
itself is never patched.

The script a case runs has, from the pool's helpers, ``Event`` (``set``, and ``wait(timeout)``
returning whether it was set), ``spawn``, ``spawn_later``, ``Timeout`` and ``GreenletExit``; and,
from the shared part, ``on_the_host``, ``start_pool``, ``stop_pool``, ``as_task``, ``report``, the
bound ``BOUND``, the host's loop ``HOST_LOOP`` (once ``on_the_host`` runs) and ``CASE_ARGS``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

# celery's own patch, as the launch's process prelude runs it, before any other import; then asyncio's
# future chaining bound to the threads it reshaped, as the host's entry binds it.
_PATCH = """
import sys

from celery import maybe_patch_concurrency

maybe_patch_concurrency(["worker", "-P", sys.argv[1]])

from tai42_kit.backend.reshaped_threads import bind_future_chaining

bind_future_chaining()
"""

_GEVENT_HELPERS = """
import gevent
from gevent import Timeout
from gevent.event import Event
from greenlet import GreenletExit

spawn, spawn_later = gevent.spawn, gevent.spawn_later
"""

_EVENTLET_HELPERS = """
import eventlet
import eventlet.event
from eventlet import Timeout
from greenlet import GreenletExit

spawn, spawn_later = eventlet.spawn, eventlet.spawn_after


class Event:
    def __init__(self):
        self._event = eventlet.event.Event()

    def set(self):
        self._event.send(True)

    def wait(self, timeout):
        return eventlet.with_timeout(timeout, self._event.wait, timeout_value=False)
"""

_HELPERS = {"gevent": _GEVENT_HELPERS, "eventlet": _EVENTLET_HELPERS}

# The plugin and the stub app the suite binds (``conftest``), imported once the process is patched.
_PLUGIN_IMPORTS = """
sys.path.insert(0, sys.argv[2])
import tests.conftest  # noqa: F401

from tai42_backend_celery.core import liveness_step
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.liveness_step import ProgressStep
from tai42_backend_celery.core.tasks import AsyncTask, tool_execution
"""

# Every wait of a case is bounded by ``BOUND`` seconds, so a case that goes wrong ends and says so.
# A green thread that blocks the whole OS thread stops every such wait, so past twice the bound the
# process dumps its threads' stacks and exits with an error.
_SHARED = """
import asyncio
import faulthandler
import importlib
import json
import threading
from types import SimpleNamespace

POOL = sys.argv[1]
CASE_ARGS = sys.argv[3:]
TaskPool = importlib.import_module(f"celery.concurrency.{POOL}").TaskPool
BOUND = 10.0
faulthandler.dump_traceback_later(2 * BOUND, exit=True)
HOST_LOOP = None


# Run ``case`` on a worker thread while the host's loop, handed to the jobs, runs here; return its
# outcome. A ``SystemExit`` or ``KeyboardInterrupt`` that ends the host's run of its loop is the
# outcome instead, as ``{"host_ended_with": repr(exit)}``.
def on_the_host(case):

    async def host():
        global HOST_LOOP
        HOST_LOOP = asyncio.get_running_loop()
        AsyncTask.share_loop(celery_app, HOST_LOOP)
        ended = HOST_LOOP.create_future()

        def worker():
            try:
                outcome = case()
            except BaseException as exc:
                HOST_LOOP.call_soon_threadsafe(ended.set_exception, exc)
            else:
                HOST_LOOP.call_soon_threadsafe(ended.set_result, outcome)

        threading.Thread(target=worker, name="case-worker", daemon=True).start()
        return await ended

    try:
        return asyncio.run(host())
    except (SystemExit, KeyboardInterrupt) as exc:
        return {"host_ended_with": repr(exc)}


def report_progress(next_within):
    pass


def start_pool(limit, task_timeout=0.0):
    liveness_step.register(celery_app, report_progress, task_timeout=task_timeout)
    pool = TaskPool(limit=limit)
    pool.start()
    consumer = SimpleNamespace(
        app=celery_app, pool=pool, timer=SimpleNamespace(call_repeatedly=lambda secs, fun: None)
    )
    ProgressStep(consumer).start(consumer)
    return pool


def stop_pool(pool):
    with Timeout(BOUND):
        pool.stop()


def as_task(request_id, coroutine, task=tool_execution):
    task.push_request(id=request_id)
    try:
        return task.run_async(coroutine)
    finally:
        task.pop_request()


def report(outcome):
    print(json.dumps(outcome))
"""

_PLUGIN_DIR = Path(__file__).parents[1]

GREEN_POOLS = [
    pytest.param("gevent", id="gevent"),
    pytest.param("eventlet", id="eventlet"),
]


def run_on_green_pool(pool_name: str, body: str, *case_args: str) -> dict[str, Any]:
    """Run ``body`` in a process patched as ``tai backend worker -P <pool_name>`` patches it; return its last JSON."""
    script = _PATCH + _HELPERS[pool_name] + _PLUGIN_IMPORTS + _SHARED + textwrap.dedent(body)
    completed = subprocess.run(
        [sys.executable, "-c", script, pool_name, str(_PLUGIN_DIR), *case_args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])
