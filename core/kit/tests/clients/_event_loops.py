"""The event loops a client test that depends on the loop runs on: asyncio and, where it exists, uvloop."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

import pytest

_LOOP_FACTORIES: list[tuple[str, Callable[[], asyncio.AbstractEventLoop]]] = [("asyncio", asyncio.new_event_loop)]
if sys.platform != "win32":
    import uvloop

    _LOOP_FACTORIES.append(("uvloop", uvloop.new_event_loop))

# Parametrizes a test's ``loop_factory`` argument with each loop, the test ids naming it.
on_every_loop = pytest.mark.parametrize(
    "loop_factory", [factory for _, factory in _LOOP_FACTORIES], ids=[name for name, _ in _LOOP_FACTORIES]
)
