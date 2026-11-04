"""The help text of the restart roll a fleet reload runs.

``tai config reload`` and ``tai fleet reload-config`` share it: both answer with the fleet reload report it
describes.
"""

from __future__ import annotations

#: How a fleet reload judges each worker for a restart and what its response reports.
RELOAD_ROLL_HELP = """\
The soft restart carries every hot key in place. An applied worker's row
names the recycle-class keys whose value differs from the one it bound
(``awaiting_respawn``); a worker whose reload was refused at its epoch
build (the build discarded before its swap) replies ``failed`` naming the
keys still drifted after the discard (``awaiting_respawn``); any other
failed reload, a failed discard among them, carries none. Every worker
whose reload built and reports such a key is then restarted one at a time
through the deployment's supervisor, the answering worker last, after the
reply once the roll converged. A worker whose reload was refused at its epoch build is never
restarted: the stored config did not build on it and a restart may fail
the same way, so it keeps serving its current config, its restart
withheld. Any worker other than the answering one whose state is unknown
(its reload failed otherwise or did not finish, it has no row, its reply
is malformed, or the bus was unreachable) is asked once it is ready, and
restarted unless its reply confirms it clean or states a refused build
(its restart then withheld). The answering worker is never asked: when
its own state is unknown, it needs its own restart, run after the reply
once the roll converged.

The response is the per-worker fleet report, the restart roll it ran
(``recycle``, ``null`` when the roll had no worker to visit: every
addressed worker confirmed no recycle-class key, or the only ones naming one
were refused at their epoch build) and the workers still
awaiting a respawn after it (``awaiting_respawn``)."""
