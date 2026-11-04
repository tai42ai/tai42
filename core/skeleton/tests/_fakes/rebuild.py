"""A generation rebuild that registers nothing, for epoch builds whose test needs no registry work."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_skeleton.app.epoch import Epoch


async def no_rebuild(epoch: Epoch) -> None:
    """Install a fresh serving core on the new generation and register nothing into it."""
    from tai42_skeleton.app import instance

    epoch.core = instance.app._build_serving_core()
