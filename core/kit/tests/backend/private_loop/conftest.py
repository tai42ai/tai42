"""What the tests of a job's private event loop share: the loop, the steps it records, a job that sleeps."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from tai42_kit.backend import PrivateLoop, private_loop

_LOGGER = "tai42_kit.backend.private_loop"


async def _sleep_then(seconds: float, value: object = None) -> object:
    await asyncio.sleep(seconds)
    return value


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, asyncio.AbstractEventLoop]]:
    """What ran, in order, each with the loop it ran on; the kit's release records itself as ``release``."""
    recorded: list[tuple[str, asyncio.AbstractEventLoop]] = []

    async def _release() -> None:
        recorded.append(("release", asyncio.get_running_loop()))

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _release)
    return recorded


@pytest.fixture
def private(steps: list[tuple[str, asyncio.AbstractEventLoop]]) -> Iterator[PrivateLoop]:
    """A private loop, closed after the test when the test left it open."""
    loop = PrivateLoop()
    try:
        yield loop
    finally:
        loop.close()
