"""A pool-child registry probe for the live turnover test.

``SERVED_TOOLS`` stands in for this process's tool registry. A prefork child
inherits it at fork time, so a task running in a child reports the registry the
child was forked with — the readback the live test asserts on. ``probe_registry``
returns ``[child_pid, sorted(tools)]`` so the test can tell WHICH child answered
and WHAT registry that child serves.
"""

from __future__ import annotations

import os

from tai42_backend_celery.core.app import celery_app

SERVED_TOOLS: set[str] = set()


@celery_app.task(name="tests.probe_registry")
def probe_registry() -> list[object]:
    return [os.getpid(), sorted(SERVED_TOOLS)]
