"""Fault-injection probe: end a named perpetual background task in this process.

A perpetual lifespan task (the worker-bus subscription, the failed-MCP re-probe loop,
the interactions expiry reaper, the sandbox reaper, the inbound-media retention reaper)
cannot be ended from outside its process, and a ``task.cancel()`` is a shutdown, not a
death, by the lifecycle's own contract. This probe induces the death in-process through
the app's own seam: it confirms the named task is one of the five run-until-cancelled
lifespan tasks and is currently alive on the live app, then either

* drives a real ``asyncio.Task`` of that name to raise and routes its completed,
  exceptioned self through the app's ``_on_perpetual_task_done`` done-callback — the
  exact production seam a real death fires (an ERROR log, the ``dead_perpetual_task``
  marker, and this process's own graceful exit so a supervised stack respawns it); or

* with ``request_exit=False``, only sets the marker the callback would set, WITHOUT the
  graceful exit, so a test can read the pre-exit ``/ready`` 503 truth over real HTTP on
  a process that stays up (the graceful-exit half is proven separately by the respawn a
  full call drives).

Probes never mock — the effects are the SUT's own lifecycle state and process signals."""

from __future__ import annotations

import asyncio
import contextlib
import os

from tai42_contract.app import tai42_app

# The five run-until-cancelled lifespan tasks, mapped to the ``LifecycleState``
# attribute that holds each one, so the probe can confirm the named task is really
# alive in this process before inducing its death.
_PERPETUAL_TASK_ATTRS = {
    "tai-worker-bus-subscription": "_bus_subscription_task",
    "tai-failed-mcp-reprobe": "_reprobe_task",
    "tai-interactions-expiry-reaper": "_interactions_reaper_task",
    "tai-sandbox-reaper": "_sandbox_reaper_task",
    "tai-media-retention-reaper": "_media_reaper_task",
}

# The reason string both paths record — the exception TYPE name a raised death yields
# via ``type(exc).__name__``, so the marker read asserted over ``/ready`` is identical
# whether the marker was set by the full done-callback or by the mark-only path.
_INJECTED_REASON = "RuntimeError"


async def _injected_perpetual_death() -> None:
    """The body of the injected task — raises so its completed self is a genuine death."""
    raise RuntimeError("injected perpetual-task death")


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_kill_perpetual_task(name: str, request_exit: bool = True) -> dict:
    """End the named perpetual task in THIS process through the real lifecycle seam.

    ``name`` must be one of the five run-until-cancelled lifespan tasks and must be
    alive in this process — a name that is not a perpetual task, or a task not currently
    running here, raises loudly rather than pretending to inject a death.

    ``request_exit`` (default ``True``) drives the full production done-callback: the
    death is logged, the ``dead_perpetual_task`` marker is set (``/ready`` then names
    the task and 503s) and this process requests its own graceful exit. ``False`` sets
    only the marker, leaving the process serving so a test can read the transient
    ``/ready`` 503 over real HTTP.
    """
    from tai42_skeleton.app import instance

    attr = _PERPETUAL_TASK_ATTRS.get(name)
    if attr is None:
        raise ValueError(f"{name!r} is not a perpetual lifespan task; expected one of {sorted(_PERPETUAL_TASK_ATTRS)}")
    app = instance.build_app()
    live = getattr(app, attr)
    if live is None or live.done():
        raise RuntimeError(
            f"perpetual task {name!r} is not alive in this process (attr {attr} is {live!r}); "
            "cannot inject a death of a task that is not running"
        )

    result = {"name": name, "reason": _INJECTED_REASON, "pid": os.getpid(), "exited": request_exit}
    if not request_exit:
        app._dead_perpetual_task = (name, _INJECTED_REASON)
        return result

    # A real task of the target name that dies with an exception, routed through the
    # app's own done-callback exactly as a genuinely-dead perpetual task would be — the
    # marker is set and the process's graceful exit is requested inside the callback.
    dead = asyncio.create_task(_injected_perpetual_death(), name=name)
    with contextlib.suppress(RuntimeError):
        await dead
    app._on_perpetual_task_done(dead)
    return result
