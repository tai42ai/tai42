"""The certification of retired serving generations: their settings are judged once they are released.

A retired generation stays reachable while a task, timer, transport or stream of the
runtime still carries the context of one of its requests: a cache's expiry timer or a
keep-alive timer for seconds, a drain-exempt stream until its client disconnects, a task
or a pooled connection one of its requests started for that task's or connection's life,
which can be the process's. All of that reaches the generation through its serving
surface (the object every request's ``scope["app"]`` points at). A retire therefore arms
a finalizer on that surface; when the surface is collected (the generation is released)
its epoch is swept for stale-config leaks on the serving loop, so every retired-epoch
settings instance still reachable then is held by something else — a captured singleton
— and is reported loudly. A generation that is never released is never judged; it stays
in the roster (:func:`retired_generations`).
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tai42_kit.settings.cache_registry import sweep_stale_settings

if TYPE_CHECKING:
    from tai42_skeleton.app.epoch import Epoch


@dataclass(frozen=True)
class RetiredGeneration:
    """A retired serving generation whose certification has not run yet.

    ``surface_alive`` is whether its serving surface is still reachable; once it is
    collected the certification is scheduled on the serving loop, and a certified
    generation leaves the roster.
    """

    number: int
    surface_alive: bool


# The retired generations awaiting certification: epoch number -> the finalizer armed on
# the generation's serving surface. Read and written on the serving loop only.
_pending: dict[int, weakref.finalize[[asyncio.AbstractEventLoop, int], object]] = {}


def retired_generations() -> tuple[RetiredGeneration, ...]:
    """The retired serving generations of this process whose certification has not run yet, oldest first."""
    return tuple(
        RetiredGeneration(number=number, surface_alive=finalizer.alive)
        for number, finalizer in sorted(_pending.items())
    )


def detach_retired_generations() -> None:
    """Disarm every pending certification: the process is leaving its serving generations behind."""
    for finalizer in _pending.values():
        finalizer.detach()
    _pending.clear()


def certify_retired_generation(retired: int) -> None:
    """Sweep the retired epoch ``retired`` for stale-config leaks and drop it from the roster.

    Never drops anything else: a retired-epoch settings instance still reachable is
    reported loudly by ``sweep_stale_settings``.
    """
    _pending.pop(retired, None)
    sweep_stale_settings(retired)


def _schedule_certification(loop: asyncio.AbstractEventLoop, retired: int) -> None:
    """Schedule the certification of ``retired`` on the serving loop.

    The finalizer of a retired generation's serving surface: it runs on whatever thread
    collects the surface, so it only schedules. A closed loop means the process stopped
    serving, and nothing is certified at exit.
    """
    if not loop.is_closed():
        loop.call_soon_threadsafe(certify_retired_generation, retired)


def arm_certification(surface: object | None, retired: int) -> None:
    """Certify the retired epoch ``retired`` once ``surface`` is collected, on the running (serving) loop.

    A generation with no serving surface (it recorded none, or it is already collected)
    is certified now. The finalizer holds the loop and the epoch number, never the
    surface or the generation.
    """
    if surface is None:
        certify_retired_generation(retired)
        return
    finalizer = weakref.finalize(surface, _schedule_certification, asyncio.get_running_loop(), retired)
    finalizer.atexit = False
    _pending[retired] = finalizer


def arm_retired_generation_certification(old: Epoch, retired: int) -> None:
    """Drop the retired generation's references and certify it once its serving surface is collected.

    The last step of a retire, on both of its tails: after the settings reset (so a
    singleton a reset hook releases is gone), the in-flight drain and the lifespan close.
    A runtime task, timer, transport or stream carrying one of its requests' contexts
    keeps the generation reachable through its serving surface, up to the process's life;
    the generation is judged once that surface is collected, never while it is reachable
    (:func:`arm_certification`).
    """
    surface = old.serving_surface()
    old.drop_serving_surface()
    arm_certification(surface, retired)
