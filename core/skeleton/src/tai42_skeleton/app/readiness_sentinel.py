"""Readiness sentinel file — the backend readiness probe's signal.

The boot-ready latch (first successful self-resync) writes this file; shutdown start
removes it. A k8s exec readiness probe tests the path, so a pod joins Service
endpoints only once its tool registry is built and leaves them the instant shutdown
begins. The path (``TAI_READY_SENTINEL_PATH``, default ``/tmp/tai-ready``) MUST be
container-local and ephemeral — a crash-restart must start unready, so it is never a
persistent mount. Bare env read, X-classified so no profile can relocate it.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

READY_SENTINEL_PATH_ENV = "TAI_READY_SENTINEL_PATH"
DEFAULT_READY_SENTINEL_PATH = "/tmp/tai-ready"


def ready_sentinel_path() -> str:
    """The readiness sentinel path from ``TAI_READY_SENTINEL_PATH`` (default
    ``/tmp/tai-ready``). Bare env read — the marker is X-classified, so a reload never
    changes it."""
    return os.environ.get(READY_SENTINEL_PATH_ENV, "").strip() or DEFAULT_READY_SENTINEL_PATH


def write_ready_sentinel() -> None:
    """Atomically create the readiness sentinel (temp write + rename). Raises on an
    unwritable path — a readiness signal that cannot be written is a loud boot fault,
    never a silently unready pod."""
    path = ready_sentinel_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("ready\n")
    os.replace(tmp, path)


def remove_ready_sentinel() -> None:
    """Remove the readiness sentinel at shutdown start. A missing file is expected (boot
    may have failed before the latch flipped); any other error is logged loudly and
    does NOT abort the remaining shutdown teardown."""
    path = ready_sentinel_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.error("readiness sentinel: failed to remove %s at shutdown start", path, exc_info=True)
