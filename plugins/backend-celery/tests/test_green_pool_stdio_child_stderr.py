"""A stdio MCP server child's stderr stream leaves the worker's descriptor 2 as it was, on a green pool.

A worker that runs a green pool (``-P gevent``, ``-P eventlet``) is patched before anything else is
imported, and the kit's :func:`~tai42_kit.transport.stdio_launch.stdio_child_stderr` is the stream
every stdio MCP server child it starts writes its stderr to. Closing that stream leaves descriptor 2
open, on the same file, and blocking, so the worker's own log lines keep reaching its standard error.

Each case runs in a fresh process patched as the launch patches it (:mod:`_green_pool_process`).
"""

from __future__ import annotations

import pytest

from ._green_pool_process import GREEN_POOLS, run_on_green_pool

_CASE = """
import os

from tai42_kit.transport.stdio_launch import stdio_child_stderr


def descriptor_2():
    try:
        os.fstat(2)
    except OSError as exc:
        return {"error": repr(exc)}
    return {"target": os.readlink("/proc/self/fd/2"), "blocking": os.get_blocking(2)}


before = descriptor_2()
stdio_child_stderr().close()
report({"before": before, "after": descriptor_2()})
"""


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_closing_the_stdio_child_stderr_stream_leaves_descriptor_2_open_unchanged_and_blocking(pool_name: str):
    outcome = run_on_green_pool(pool_name, _CASE)

    assert outcome["before"]["blocking"] is True
    assert outcome["after"] == outcome["before"]
