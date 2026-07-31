"""Run a uvicorn app in a background thread of the pytest process.

The LLM stub and the net fixtures are harness infrastructure, not the system
under test, so they live in-process on allocated ports rather than as spawned
processes or compose services."""

from __future__ import annotations

import threading
from typing import Any

import uvicorn

from tai42_e2e.waiting import wait_for


class ThreadedServer:
    """A uvicorn server bound to an ASGI app, started/stopped from a thread."""

    def __init__(self, app: Any, host: str, port: int) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.host = host
        self.port = port

    def start(self) -> None:
        self._thread.start()
        wait_for(
            lambda: self._server.started,
            deadline=10.0,
            message=f"threaded server never started on {self.host}:{self.port}",
        )

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)

    def __enter__(self) -> ThreadedServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
