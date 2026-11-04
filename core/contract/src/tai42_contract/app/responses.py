"""Shared response models for the ``/api/*`` success surface.

The pydantic-only contract layer is the home for response shapes more than one
package (skeleton operations, plugins) declares on a
route, so a model shared across the seam lives here and is imported by full
submodule path (``from tai42_contract.app.responses import ...``). Each model
DESCRIBES the payload a route returns today, wrapped by the caller in the
``{"data": ...}`` success envelope — it never re-declares the envelope and never
reshapes a wire body.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, JsonValue, RootModel


class FanoutWorkerResult(BaseModel):
    """One worker's verdict within a multi-worker fan-out report.

    ``payload`` carries a query op's per-worker data; ``error`` a failed apply's
    message; ``detail`` the publisher's note for a computed missing/departed/
    timed-out verdict. ``outcome`` is the worker's terminal outcome as its wire
    string.
    """

    name: str
    outcome: str
    payload: JsonValue | None = None
    error: str | None = None
    detail: str | None = None


class FanoutSummary(BaseModel):
    """The mode-tagged fan-out summary embedded under a mutation response's ``fanout`` field.

    ``mode`` selects the shape: ``local-only`` carries only ``note`` (a lone
    worker reached no sibling); ``fleet`` and ``unreachable`` carry the per-worker
    broadcast report (``op``/``reachable``/``local_only``/``results``/``error``),
    the ``unreachable`` variant having no worker list, only ``error``. The
    mode-variant fields are optional so one model describes every mode without
    reshaping any wire payload.
    """

    mode: str
    note: str | None = None
    op: str | None = None
    reachable: bool | None = None
    local_only: bool | None = None
    results: list[FanoutWorkerResult] | None = None
    error: str | None = None


class RecycleEntry(BaseModel):
    """One worker line of a fleet recycle roll.

    ``status`` is ``recycled`` (confirmed on reality), ``timed-out`` or ``failed`` (the target the
    roll stopped at), ``self-deferred`` (a restart the roll cannot confirm: the writer's own
    post-response self-exit, or a target whose exit is held by its own in-flight change and runs
    once that change's reply is answered), or ``withheld``
    (a worker asked once ready whose reload was refused at its epoch build — the build discarded
    before its swap — yet reports a recycle-class key: the stored config did not build on it and a
    restart may fail the same way, so the roll leaves it running on its current config). ``keys``
    are the recycle-class keys that worker's reload reported as moved off the value it bound them
    under — the reason it was restarted, or for a ``withheld`` worker the keys it still awaits;
    empty for a worker restarted because its state stayed unknown. A worker other than the writer
    whose state was unknown is asked once it is ready and restarted unless its reply confirms it
    clean or states a refused build (its line then ``withheld``). The writer is never asked at its
    own roll: when its own state is unknown, its restart is left to its post-response self-exit,
    armed only when the roll converged and the reply can carry one (then its ``self-deferred``
    line). ``detail`` names what left that
    worker's state unknown (its reload outcome, no report row, a malformed reply, an unreachable
    bus) with what it answered when asked once ready, and why a ``timed-out`` / ``failed`` target
    did not converge, joined with ``"; "``; ``null`` when none applies.
    """

    name: str
    kind: str
    status: str
    generation_before: int
    keys: list[str]
    detail: str | None = None


class RecycleStop(BaseModel):
    """Where a profile apply's recycle roll stopped and why.

    ``name`` is the target the roll stopped at (its line in ``recycle`` carries the
    target's own outcome), or ``null`` when the bus could not be read before a target was
    reached.
    """

    kind: str
    name: str | None
    detail: str


class FreshLife(BaseModel):
    """One newly-ready worker life observed since the pre-apply snapshot."""

    name: str
    kind: str
    generation: int


class RecycleRoll(BaseModel):
    """The report of a fleet recycle roll a reload ran for the workers that needed a restart.

    A worker needs a restart when its reload reported a recycle-class key moved off the value it
    bound. A worker whose reload was refused at its epoch build (the build discarded before its
    swap) is never restarted, even when it reports such a key: the stored config did not build on it
    and a restart may fail the same way, so it keeps serving its current config; a failed discard is
    no refused build, and leaves the worker's state unknown. A worker other than the writer whose
    state was unknown is asked once it is ready and restarted unless its reply confirms it clean or
    states a refused build (its line then ``withheld``). The writer is never asked at its own roll:
    when its own state is unknown, its restart is left to its post-response self-exit, armed only
    when the roll converged and the reply can carry one (then its ``self-deferred`` line).
    ``recycle`` the worker lines of the roll
    (the writer's own ``self-deferred`` line when its post-response self-exit is armed); ``skipped``
    the workers whose reload confirmed no such key, at the reload or when asked once ready, left
    running; ``fresh`` the new ready lives seen since the change began; ``recycle_stopped`` is
    ``null`` when the roll ran to its end, otherwise where it stopped and why. Names only — never
    env values.
    """

    recycle: list[RecycleEntry]
    skipped: list[str]
    fresh: list[FreshLife]
    recycle_stopped: RecycleStop | None = None


class AwaitingWorker(BaseModel):
    """One worker an operation names as still awaiting a respawn.

    Its recycle-class ``keys`` changed in the stored env since its process bound them (at boot, or
    when the plugin that declares them was loaded), and nothing in the operation respawns it.
    """

    name: str
    keys: list[str]


class ApplyResponse(BaseModel):
    """The standard mutation-op response: this worker's local reload result merged with the fleet fan-out summary.

    ``env_keys`` is the count of env keys the reload loaded. ``recycle`` is the fleet recycle roll
    the change ran — ``null`` when it had no worker to visit: every worker's reload confirmed no
    recycle-class key moved off its bound value, or the only ones naming one were refused at their epoch
    build or are the writer whose own restart cannot follow as its exit. ``awaiting_respawn`` is the
    operation's verdict: the workers still awaiting a respawn after it (name + keys) — when the roll
    converged and this worker recycles itself, only the workers whose reload was refused at their epoch
    build: the stored config did not build on them and a restart may fail the same way, so the roll
    leaves them running and lists them; a worker the roll did not reach keeps its keys.
    ``roll_deferred`` is ``true``
    when the roll is not judged in this reply: ``roll_deferred_to`` then names the worker that
    carries it out after the reply (``recycle`` is ``null`` and ``awaiting_respawn`` empty), and
    is ``null`` otherwise. ``span_release_error`` is ``null`` when the config store released its
    exclusive write span after the persist; otherwise the store's error — the change landed and
    the reload, broadcast and roll reported here still ran, but the store may still hold its write
    span until the writer process exits, so a later write can block or be refused until then.
    """

    status: str
    env_keys: int
    awaiting_respawn: list[AwaitingWorker] = Field(default_factory=list[AwaitingWorker])
    fanout: FanoutSummary
    recycle: RecycleRoll | None = None
    roll_deferred: bool = False
    roll_deferred_to: str | None = None
    span_release_error: str | None = None


class ProfileApplyResponse(BaseModel):
    """The dedicated profile-apply response.

    ``hot`` are the hot-class diff key names; ``recycle`` the worker lines of the recycle roll
    (empty when the roll had no worker to visit: every worker's reload confirmed no recycle-class
    key moved off its bound value, or the only ones naming one were refused at their epoch build or are
    the writer whose own restart cannot follow as its exit);
    ``skipped`` the workers so confirmed, left running; ``fresh`` the new ready lives seen since
    the pre-apply snapshot; ``refused`` is empty on success
    (any refusal aborts the pipeline before a response is built); ``recycle_stopped`` is ``null``
    when the roll ran to its end, otherwise where it stopped and why; ``awaiting_respawn`` the
    workers still awaiting a respawn after the apply (name + keys; when the roll converged, only
    the workers whose reload was refused at their epoch build: the stored config did not build on them
    and a restart may fail the same way, so the roll leaves them running and lists them);
    ``fanout`` the reload broadcast's fleet summary. Names only — never env values.
    ``roll_deferred`` is ``true`` when the roll is not judged in this reply: ``roll_deferred_to``
    then names the worker that carries it out after the reply (``recycle`` and
    ``awaiting_respawn`` empty), and is ``null`` otherwise.
    ``span_release_error`` is ``null`` when the config store released its
    exclusive write span after the persist; otherwise the store's error — the change landed and
    the reload, broadcast and roll reported here still ran, but the store may still hold its write
    span until the writer process exits, so a later write can block or be refused until then.
    """

    hot: list[str]
    recycle: list[RecycleEntry]
    skipped: list[str]
    fresh: list[FreshLife]
    refused: list[str]
    recycle_stopped: RecycleStop | None = None
    awaiting_respawn: list[AwaitingWorker] = Field(default_factory=list[AwaitingWorker])
    roll_deferred: bool = False
    roll_deferred_to: str | None = None
    span_release_error: str | None = None
    fanout: FanoutSummary


class DeleteResult(BaseModel):
    """The recurring delete-confirmation shape.

    ``id`` and ``user_id`` are both optional so the one model covers every delete door's variant
    (``{deleted, id}`` or ``{deleted, user_id}``) without a site reshaping its body.
    """

    deleted: bool
    id: str | None = None
    user_id: str | None = None


class OpaqueJson(RootModel[JsonValue]):
    """The deliberate "any JSON value" marker for a genuinely-open body.

    A tool or upstream passthrough whose shape is not fixed. A named subclass, never a bare alias:
    the emitter registers a component under ``model.__name__``, so only a real class yields a stable,
    unique component name. Reach for it ONLY for a genuinely-open body, never as a shortcut past
    authoring a real model.
    """
