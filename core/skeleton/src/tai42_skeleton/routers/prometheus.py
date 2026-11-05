"""Prometheus multiprocess directory lifecycle and exposition rendering.

The mcp_app master, the backend worker, and the metrics server form ONE run
family over a single shared multiproc dir (``PROMETHEUS_MULTIPROC_DIR``) and are
restarted TOGETHER. Ownership of the dir is split so no live worker's mmap files
are yanked mid-run:

- ``wipe_prometheus_multiproc_dir`` — master-only, called once before workers are
  spawned. It clears stale db files from previous runs under an exclusive lock,
  guarded by a per-run sentinel so only the first caller of a run wipes. It empties
  the dir and never removes the dir itself, so a process that ensures or writes
  into the dir meanwhile never finds it gone.
- ``init_prometheus_multiproc_dir`` — every reader/worker path; ensures the dir
  exists without wiping, so the metrics server and backend worker never destroy a
  populated dir.

Restart semantics (accepted, not engineered around): restarting the mcp_app
master mid-run re-runs the single wipe while a still-running backend worker holds
its mmap files open, silently orphaning those counters until the family restarts
together. A db file such a worker creates while the wipe runs is not one the wipe
found, so it stays. This is the standard prometheus multiproc operating model — the
dir is cleared once at fresh deploy/boot, not per process.
"""

import logging
import os
import shutil
import sys

from prometheus_client import REGISTRY, CollectorRegistry, generate_latest, values
from prometheus_client.multiprocess import MultiProcessCollector

from tai42_skeleton.routers.metrics_settings import metrics_settings

logger = logging.getLogger(__name__)

# Records whether the mode this process serves has been logged, so the fork is
# announced exactly once (on the first scrape) rather than on every scrape.
_mode_logged = False

# Cross-platform exclusive file locking for the master's wipe-once protocol and the
# health watch's alerted-totals file. Both branches provide REAL, genuinely-blocking
# mutual exclusion (no silent no-op): Windows locks the 1-byte region via
# ``msvcrt.locking`` and POSIX via ``fcntl.flock``, so only one process at a time
# holds the lock and the wipe below runs under exclusive access.
if sys.platform == "win32":
    import msvcrt

    def _lock_exclusive(fd: int) -> None:
        # ``LK_LOCK`` blocks ~10 retries then raises; loop until the 1-byte region
        # is held so acquisition is genuinely blocking, matching POSIX ``LOCK_EX``.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            except OSError:
                continue
            else:
                return

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_exclusive(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _multiproc_lock_path(metrics_dir: str) -> str:
    """Path of the exclusive lock file guarding the multiproc dir's lifecycle.

    The lock sits *beside* the dir, never inside it: the dir may carry a trailing
    slash, which would place the lock at ``dir/.lock`` where the wipe would remove it
    while it is held; stripping the slash keeps the lock next to the dir so it survives
    the wipe. The parent directory is ensured to exist so the lock file can be created
    (a parentless relative dir has no parent to create). The master wipe and the
    monitoring health watch's alerted-totals file lock on this one path, so the wipe
    never clears the dir while that file is being read or replaced.
    """
    parent_dir = os.path.dirname(metrics_dir.rstrip("/"))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    return f"{metrics_dir.rstrip('/')}.lock"


def init_prometheus_multiproc_dir():
    """Ensure the multiproc dir exists WITHOUT wiping it.

    Every reader/worker path (the backend worker at boot, the metrics server's
    ``create_app``) runs this: it creates the dir if missing but never removes a
    populated one, so a live worker's mmap files survive. The once-per-run wipe is
    the master's job (``wipe_prometheus_multiproc_dir``); it never removes the dir,
    so this ensure-exists needs no lock against it.
    """
    metrics_dir = metrics_settings().prometheus_multiproc_dir
    os.makedirs(metrics_dir, exist_ok=True)
    return metrics_dir


def _clear_dir_entries(metrics_dir: str) -> None:
    """Remove every entry the multiproc dir holds now, keeping the dir itself.

    The entries are listed once, before any removal: a file a live writer of the run
    family creates while the clear runs (``prometheus_client`` creates a process's
    ``<type>_<pid>.db`` at its first sample of that type, with no lock) is not one of
    them, so it stays.
    """
    with os.scandir(metrics_dir) as listing:
        entries = list(listing)
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def wipe_prometheus_multiproc_dir():
    """Master-only: clear the multiproc dir once per run before workers spawn.

    Removes stale db files left by previous runs so a scrape does not merge dead
    processes' counters. Guarded by an exclusive lock and a per-run sentinel so
    exactly one caller of a run wipes; the backend worker and metrics server never
    call this. The wipe empties the dir in place and never removes the dir itself:
    the run family's writers create their db files without any lock, and a dir that
    is never deleted is one their file creation and the readers' ensure-exists can
    always land in.
    """
    metrics_dir = metrics_settings().prometheus_multiproc_dir
    lock_file_path = _multiproc_lock_path(metrics_dir)

    with open(lock_file_path, "w") as lock_file:
        try:
            # Acquire an exclusive lock (blocking).
            # The first worker to grab this holds it; others wait here.
            _lock_exclusive(lock_file.fileno())

            # The sentinel records this run's id: every worker of one run shares
            # it (the master stamps ``TAI_METRICS_RUN_ID`` before forking), so
            # only the first wipes; a NEW run gets a new id, so the wipe re-runs
            # and dead workers' db files don't accumulate across restarts. A pid
            # (``getppid``) is not enough — the OS reuses pids, and a single-worker
            # run launched repeatedly from the same parent would share one, wrongly
            # skipping the wipe. The pid is only a last-resort fallback for a
            # direct caller with no master-stamped id.
            run_marker = os.environ.get("TAI_METRICS_RUN_ID") or str(os.getppid())
            sentinel = os.path.join(metrics_dir, ".init_done")
            if os.path.exists(sentinel):
                with open(sentinel) as f:
                    if f.read().strip() == run_marker:
                        return metrics_dir

            # We are the designated initializer (first one here this run).
            # Clear the directory; a failed clear must raise, not get papered
            # over by writing the sentinel anyway.
            os.makedirs(metrics_dir, exist_ok=True)
            _clear_dir_entries(metrics_dir)

            # Create the sentinel so subsequent workers (waiting on lock) skip the wipe
            with open(sentinel, "w") as f:
                f.write(run_marker)

        finally:
            # Release the lock so other workers can proceed
            _unlock(lock_file.fileno())

    return metrics_dir


def multiproc_active() -> bool:
    """Whether ``prometheus_client`` froze the multiprocess mmap value backend.

    Reads the frozen ``values.ValueClass`` — the single source of truth for which
    mode this process is in, since the backend is chosen once at the first
    ``prometheus_client`` import from ``PROMETHEUS_MULTIPROC_DIR`` and never changes
    after. Both the writer assert and the render fork trust this same fact, so
    neither can disagree with where counters were actually written.
    """
    return bool(getattr(values.ValueClass, "_multiprocess", False))


def assert_multiproc_value_class() -> None:
    """Fail loudly in a WRITER process if multiproc mode did not activate.

    ``prometheus_client`` freezes its value backend at first import: the mmap
    backend (counters written to per-process db files in the shared dir) only when
    ``PROMETHEUS_MULTIPROC_DIR`` was already set, otherwise an in-process mutex
    backend whose counters are invisible to every ``/metrics`` scrape. A writer
    that froze the mutex backend would silently lose every tool counter, so raise
    rather than serve into that void.
    """
    if not multiproc_active():
        raise RuntimeError(
            "prometheus_client froze the in-process value backend "
            f"({values.ValueClass.__name__}) instead of the multiprocess mmap backend: "
            "PROMETHEUS_MULTIPROC_DIR was not set before prometheus_client was first "
            "imported, so tool counters written here would be absent from every scrape."
        )


def render_multiproc_metrics() -> bytes:
    """Collect the multiproc db and render the Prometheus exposition text.

    The shared body behind every multiproc ``/metrics`` scrape.
    """
    registry = CollectorRegistry()
    MultiProcessCollector(registry)
    return generate_latest(registry)


def render_metrics() -> bytes:
    """Render the Prometheus exposition for THIS process's ``/metrics`` scrape.

    The fork is decided by the frozen value backend (:func:`multiproc_active`),
    never by sniffing the environment, so it can never disagree with where counters
    were actually written: a multiproc process merges every worker's mmap db, an
    in-process process renders the default registry the counters landed in.
    """
    global _mode_logged
    multiproc = multiproc_active()
    if not _mode_logged:
        _mode_logged = True
        logger.info(
            "Serving /metrics in %s mode, decided by the frozen prometheus_client value backend %s.",
            "multiproc" if multiproc else "in-process",
            values.ValueClass.__name__,
        )
    if multiproc:
        return render_multiproc_metrics()
    return generate_latest(REGISTRY)
