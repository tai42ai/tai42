"""The errors the config pipeline raises that its callers outside the pipeline catch by type."""

from __future__ import annotations


class OrphanEnvWriteError(RuntimeError):
    """A combined env+manifest op whose manifest persist FAILED after the env write already landed.

    Env-first/manifest-second ordering: on a manifest-persist failure there is NO
    rollback — the env write STANDS as an inert, re-runnable orphan (rolling it back
    could leave a still-referenced marker resolving silently to ``"N/A"``). The message
    NAMES the orphan env key(s) and the manifest pointer they now reference no persisted
    marker at, and states the env write stands / re-run to complete. A ``RuntimeError``
    (not a ``ValueError``) so the op layer does not fold it into a 400 — a partial
    failure is loud, not a client input error.
    """
