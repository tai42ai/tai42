"""Every exec-attachment test runs on asyncio and on uvloop.

The attachment runs on whichever loop its caller runs: uvloop under uvicorn, asyncio elsewhere.
The two loops build their socket transports differently, so each test runs on both.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
import uvloop
from pytest_asyncio.plugin import LoopFactory


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> Mapping[str, LoopFactory]:
    return {"asyncio": asyncio.new_event_loop, "uvloop": uvloop.new_event_loop}
