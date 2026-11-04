"""A celery worker's embedded beat (``-B``): its own liveness file, which the worker's tick reads.

celery runs the beat that ``-B`` embeds in a child process forked from the worker (or, where
multiprocessing is unavailable, in a thread of the worker), built from the worker's app. That beat
runs the reporting scheduler, which reports each tick through the reporter the app carries into
``<TAI_BACKEND_LIVENESS_PATH>-beat`` with the beat's own pid: one file per writer, so neither
process's stall hides behind the other's reports. The worker's progress tick reads that file and
reports nothing while the beat's promise is overdue, so the one probe on the worker's file covers
both, and a restart of the container brings back both.

The worker watches the beat it builds (:class:`BeatWatchStep`, a step of the worker's own
blueprint, so the watch holds whether or not the worker's consumer ever starts). The beat child
never runs the worker's unwind, so the worker removes the beat's file when it stops, once the
beat has ended: :func:`release` ends a beat still running (celery's own stop step leaves it
running when the worker stops before it fully started) and reaps it first.

The beat's file, its grace and the beat being watched live on the app the worker runs
(``HostedCelery.embedded_beat``), which celery hands the watch step as its parent's ``app``: a
config reload re-imports this module while the worker runs, and the tick and the release still
read what the worker's build and its beat step set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from celery import Celery, bootsteps
from celery.worker.components import Beat
from tai42_kit.liveness import BackendLivenessSettings, LivenessFile, check_liveness

from tai42_backend_celery.core import beat_scheduler

if TYPE_CHECKING:
    from tai42_backend_celery.core.app import HostedCelery

# Seconds the worker waits for its embedded beat to end at shutdown, once it has asked it to.
_JOIN_SECONDS = 10.0


@dataclass
class EmbeddedBeat:
    """A worker's embedded beat as the worker reads it: its liveness file, its grace, and the beat service watched."""

    file: LivenessFile
    grace: float
    # The beat service celery's ``Beat`` step built, ``None`` until it is built or without ``-B``.
    service: Any = None


def embedded_beat_path(settings: BackendLivenessSettings) -> Path:
    """Where a worker's embedded beat writes its liveness file: ``<path>-beat``, next to the worker's own."""
    return settings.path.with_name(settings.path.name + "-beat")


def register(app: HostedCelery, settings: BackendLivenessSettings) -> None:
    """Hand ``app``'s beat a reporter that writes the embedded beat's file, and watch ``app``'s workers' beat.

    Called before the worker boots: the beat child is forked from the worker after this, with the
    app and its reporter; the file records the pid of the process that writes it, so the child's
    reports carry its own pid. A worker without ``-B`` never ticks the reporter. The file, the
    grace and the watch are set on ``app`` as its ``embedded_beat``. A second call keeps the file
    and the reporter of the first and takes the grace.
    """
    state = app.embedded_beat
    if state is None:
        state = EmbeddedBeat(file=LivenessFile(embedded_beat_path(settings), settings.interval), grace=settings.grace)
        app.embedded_beat = state
    else:
        state.grace = settings.grace
    beat_scheduler.register(app, state.file.report)
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
        watch(parent.app, parent.beat)


def watch(app: HostedCelery, beat: Any) -> None:
    """Read the liveness of ``beat``, ``app``'s worker's embedded beat service, or of none when it is ``None``.

    Raises ``RuntimeError`` when :func:`register` has not run for ``app``: the beat has no file to read.
    """
    state = app.embedded_beat
    if state is None:
        raise RuntimeError(
            "the celery worker watches an embedded beat with no liveness file registered: register() runs from "
            "the worker runtime's build"
        )
    state.service = beat


def check(app: HostedCelery) -> None:
    """Raise ``RuntimeError`` when ``app``'s watched embedded beat has not kept the promise of its last tick."""
    state = app.embedded_beat
    if state is None or state.service is None:
        return
    reading = check_liveness(state.file.path, state.grace)
    if not reading.alive:
        raise RuntimeError(
            f"the embedded beat (-B) is {reading.describe()}; this worker reports nothing until its beat ticks again"
        )


def release(app: HostedCelery) -> None:
    """End ``app``'s embedded beat if it still runs, reap it, then remove its liveness file.

    A beat still running after the bounded wait raises ``RuntimeError`` and its file stays, as a
    live process still owns it. Nothing is released for an app :func:`register` never ran for.
    """
    state = app.embedded_beat
    if state is None:
        return
    beat = state.service
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
        state.service = None
    state.file.remove()


def _ask_to_end(beat: Any) -> None:
    """Ask a running embedded beat to end, without waiting on it."""
    terminate = getattr(beat, "terminate", None)
    if terminate is not None:
        # A forked beat: its service runs in the child, which the signal ends.
        terminate()
    else:
        # A threaded beat: its service loop runs in this process and ends at its next pass.
        beat.service.stop()
