"""A plugin worker in its own process, consuming one queue: run as a program by the live tests.

Usage: ``python _worker_process.py QUEUE HOSTNAME``.

The suite's stub app (``conftest``) is bound before the plugin is imported, as in the test
process; the plugin app is built from the ``CELERY_*`` environment, as the test process builds its own,
and its ``solo`` worker consumes ``QUEUE`` under ``HOSTNAME`` while the hub runs as the fork
gate's engine loop, as the worker runtime runs it. It runs apart from the test process because
Celery keeps a worker's running and revoked tasks process-wide: a second worker in the test
process would answer for the first one's tasks. The process stops warmly on ``SIGTERM``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests.conftest  # noqa: F401

from tai42_backend_celery.core import liveness_step
from tai42_backend_celery.core.app import create_celery_app


def main(queue: str, hostname: str) -> None:
    """Run the worker until the process is told to stop."""
    app = create_celery_app()
    argv = ["worker", "-P", "solo", "-Q", queue, "-n", hostname, "-l", "ERROR"]
    with liveness_step.hub_as_engine_loop(app):
        app.worker_main([*argv, "--without-gossip", "--without-mingle", "--without-heartbeat"])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
