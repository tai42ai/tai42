"""A celery worker's embedded beat (``-B``): its own liveness file, which the worker's tick reads.

celery runs the beat that ``-B`` embeds in a child process forked from the worker (or, where
multiprocessing is unavailable, in a thread of the worker). That beat runs the reporting scheduler,
which reports each tick into ``<TAI_BACKEND_LIVENESS_PATH>-beat`` with the beat's own pid: one
file per writer, so neither process's stall hides behind the other's reports. The worker's
progress tick reads that file and reports nothing while the beat's promise is overdue, so the one
probe on the worker's file covers both, and a restart of the container brings back both.

The worker watches the beat it builds (:class:`BeatWatchStep`, a step of the worker's own
blueprint, so the watch holds whether or not the worker's consumer ever starts). The beat child
never runs the worker's unwind, so the worker removes the beat's file when it stops, once the
beat has ended: :func:`release` ends a beat still running (celery's own stop step leaves it
running when the worker stops before it fully started) and reaps it first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from celery import Celery, bootsteps
from celery.worker.components import Beat
from tai42_kit.liveness import BackendLivenessSettings, LivenessFile, check_liveness

from tai42_backend_celery.core import beat_scheduler

# Seconds the worker waits for its embedded beat to end at shutdown, once it has asked it to.
_JOIN_SECONDS = 10.0

_file: LivenessFile | None = None
_grace: float = 0.0
_beat: Any = None


def embedded_beat_path(settings: BackendLivenessSettings) -> Path:
    """Where a worker's embedded beat writes its liveness file: ``<path>-beat``, next to the worker's own."""
    return settings.path.with_name(settings.path.name + "-beat")


def register(app: Celery, settings: BackendLivenessSettings) -> None:
    """Hand the beat scheduler a reporter that writes the embedded beat's file, and watch ``app``'s workers' beat.

    Called before the worker boots: the beat child is forked after this and inherits the reporter;
    the file records the pid of the process that writes it, so the child's reports carry its own
    pid. A worker without ``-B`` never ticks the reporter. A second call keeps the reporter of the
    first.
    """
    global _file, _grace
    if _file is None:
        _file = LivenessFile(embedded_beat_path(settings), settings.interval)
    _grace = settings.grace
    beat_scheduler.register(_file.report)
    worker_steps(app).add(BeatWatchStep)


def worker_steps(app: Celery) -> set[type[bootsteps.Step]]:
    """The custom bootsteps ``app`` adds to every worker's own blueprint.

    celery declares ``steps = None`` on the class and sets a ``defaultdict(set)`` in ``__init__``.
    """
    return cast("dict[str, set[type[bootsteps.Step]]]", app.steps)["worker"]


class BeatWatchStep(bootsteps.Step):
    """Watch the embedded beat the worker builds, or none when it runs without ``-B``.

    A step of the worker's blueprint, built with the worker after celery's ``Beat`` step.
    """

    requires = (Beat,)

    def create(self, parent: Any) -> None:
        """Watch ``parent.beat``, which celery's ``Beat`` step has just built (``None`` without ``-B``)."""
        watch(parent.beat)


def watch(beat: Any) -> None:
    """Read the liveness of ``beat``, the worker's embedded beat service, or of none when it is ``None``."""
    global _beat
    _beat = beat


def check() -> None:
    """Raise ``RuntimeError`` when the watched embedded beat has not kept the promise of its last tick."""
    if _beat is None or _file is None:
        return
    reading = check_liveness(_file.path, _grace)
    if not reading.alive:
        raise RuntimeError(
            f"the embedded beat (-B) is {reading.describe()}; this worker reports nothing until its beat ticks again"
        )


def release() -> None:
    """End the embedded beat if it still runs, reap it, then remove its liveness file.

    A beat still running after the bounded wait raises ``RuntimeError`` and its file stays, as a
    live process still owns it.
    """
    global _beat
    beat = _beat
    if beat is not None:
        if beat.is_alive():
            _ask_to_end(beat)
            beat.join(_JOIN_SECONDS)
        if beat.is_alive():
            pid = getattr(beat, "pid", None)
            who = f"pid {pid}" if pid is not None else f"thread {beat.name}"
            raise RuntimeError(
                f"the embedded beat (-B, {who}) did not end within {_JOIN_SECONDS:g}s of the worker stopping"
            )
        _beat = None
    if _file is not None:
        _file.remove()


def _ask_to_end(beat: Any) -> None:
    """Ask a running embedded beat to end, without waiting on it."""
    terminate = getattr(beat, "terminate", None)
    if terminate is not None:
        # A forked beat: its service runs in the child, which the signal ends.
        terminate()
    else:
        # A threaded beat: its service loop runs in this process and ends at its next pass.
        beat.service.stop()
