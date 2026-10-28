"""The graceful-exit primitive for a worker.

A recycled worker replies ``applied`` and then exits cleanly so its supervisor respawns it and
the fresh process loads new store env at boot. The exit is a SIGTERM delivered to the worker's
process: its already-installed graceful handler runs, so in-flight work finishes before the
process ends — never a hard kill. For a serve worker that is uvicorn's shutdown, which stops
accepting and lets in-flight requests send their responses; the worker ends its event streams,
the ones MCP replies ride, only once those replies are written
(:mod:`~tai42_skeleton.app.serve_exit`). For a backend worker it is the backend's drain, which
lets the in-flight job finish and record its result.

Three callers use it: the recycle handler arms it on the bus's post-reply slot (fired AFTER the
terminal reply); an env-change door arms it as the writer's own exit after a converged roll
(``writer_exit_response``), run after the body flushes on the HTTP edge and once the call
returned on the projected tool edge; and the lifespan calls it when a perpetual background task
dies. The writer's exit aims at the bus member the process acts for, so a tool door run inside a
forked job child stops the member worker that forked it, after that job.
"""

from __future__ import annotations

import os
import signal


def request_graceful_exit(pid: int) -> None:
    """Deliver SIGTERM to the worker process ``pid``, starting its graceful stop."""
    os.kill(pid, signal.SIGTERM)
