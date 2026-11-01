"""Every host-exec test runs on asyncio and on uvloop, and every host child it spawned is killed when it ends.

A host exec runs on whichever loop its caller runs: uvloop under uvicorn, asyncio elsewhere. The
two loops spawn and pipe a subprocess differently, so each test runs on both.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest
import uvloop
from pytest_asyncio.plugin import LoopFactory

from .children import SPAWNED


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> Mapping[str, LoopFactory]:
    return {"asyncio": asyncio.new_event_loop, "uvloop": uvloop.new_event_loop}


@pytest.fixture(autouse=True)
async def _kill_what_the_test_spawned() -> AsyncIterator[None]:
    yield
    while SPAWNED:
        await SPAWNED.pop().kill()
