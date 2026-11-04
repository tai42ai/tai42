"""The serving loop the lifecycle state binds: bound for ``app_context``, unbound when it leaves, and
never inherited by a forked child."""

from __future__ import annotations

import asyncio
import os

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest

from ._doubles import _Mixin


def _fork_and_read_the_serving_loop(m: _Mixin) -> str:
    """Fork, and report from the child whether ``m`` has a serving loop bound there.

    Runs on a worker thread, as a backend pool forks its job children off the serving loop.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits through os._exit
        try:
            os.close(read_fd)
            os.write(write_fd, b"unbound" if m.serving_loop() is None else b"bound")
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as pipe:
            return pipe.read()
    finally:
        os.waitpid(pid, 0)


async def test_a_child_forked_while_a_serving_loop_is_bound_has_none_bound() -> None:
    m = _Mixin()
    m.bind_serving_loop()
    try:
        assert m.serving_loop() is asyncio.get_running_loop()
        assert await asyncio.to_thread(_fork_and_read_the_serving_loop, m) == "unbound"
        assert m.serving_loop() is asyncio.get_running_loop()
    finally:
        m.unbind_serving_loop()


def test_app_context_binds_its_loop_as_the_serving_loop_and_unbinds_it_when_it_leaves() -> None:
    async def run() -> asyncio.AbstractEventLoop | None:
        async with instance.app.app_context(Manifest.model_validate({})):
            bound = instance.app.serving_loop()
            assert bound is asyncio.get_running_loop()
        return instance.app.serving_loop()

    assert asyncio.run(run()) is None
