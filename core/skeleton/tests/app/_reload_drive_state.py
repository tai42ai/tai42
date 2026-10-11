"""Shared state of the reload-drive fixture tools, outside the module a reload re-imports.

A served test resets it before it drives the tools; the tools read it at call time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class DriveState:
    """The events a served test steers the fixture tools with, and what the drives observed."""

    held_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_held: asyncio.Event = field(default_factory=asyncio.Event)
    driver_swapped: asyncio.Event = field(default_factory=asyncio.Event)
    release_driver: asyncio.Event = field(default_factory=asyncio.Event)
    hold_driver: bool = False
    drives: list[dict[str, object]] = field(default_factory=list)


STATE = DriveState()


def reset(*, hold_driver: bool = False) -> DriveState:
    """Start a test with fresh events; ``hold_driver`` makes the driving call answer only once released."""
    global STATE
    STATE = DriveState(hold_driver=hold_driver)
    return STATE
