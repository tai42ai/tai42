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
    """One worker line in a profile-apply report.

    ``status`` is ``recycled`` (confirmed on reality), ``timed-out`` or ``failed`` (the
    target the roll stopped at), or ``self-deferred`` (the applier's own post-response
    self-exit). ``detail`` names why a ``timed-out`` / ``failed`` target did not
    converge, and is ``null`` otherwise.
    """

    name: str
    kind: str
    status: str
    generation_before: int
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
    """The report of a fleet recycle roll an env change carrying a recycle-class key ran.

    ``recycle`` the worker lines of the roll (the writer's own ``self-deferred`` line when its
    post-response self-exit is armed); ``fresh`` the new ready lives seen since the change began;
    ``recycle_stopped`` is ``null`` when the roll ran to its end, otherwise where it stopped and
    why. Names only — never env values.
    """

    recycle: list[RecycleEntry]
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

    ``env_keys`` is the count of env keys the reload loaded. ``recycle`` is the fleet recycle
    roll the change ran — ``null`` when it carried no recycle-class key. ``awaiting_respawn`` is
    the operation's verdict: the workers still awaiting a respawn after it (name + keys) — empty
    when the change rolled a recycle that converged and this worker recycles itself; a worker the
    roll did not reach, and every listed worker of a change that rolls nothing, keep their keys.
    """

    status: str
    env_keys: int
    awaiting_respawn: list[AwaitingWorker] = Field(default_factory=list[AwaitingWorker])
    fanout: FanoutSummary
    recycle: RecycleRoll | None = None


class ProfileApplyResponse(BaseModel):
    """The dedicated profile-apply response.

    ``hot`` are the hot-class diff key names; ``recycle`` the worker lines of the recycle roll;
    ``fresh`` the new ready lives seen since the pre-apply snapshot; ``refused`` is empty on success
    (any refusal aborts the pipeline before a response is built); ``recycle_stopped`` is ``null``
    when the roll ran to its end, otherwise where it stopped and why; ``awaiting_respawn`` the
    workers still awaiting a respawn after the apply (name + keys; empty when the roll converged);
    ``fanout`` the reload broadcast's fleet summary. Names only — never env values.
    """

    hot: list[str]
    recycle: list[RecycleEntry]
    fresh: list[FreshLife]
    refused: list[str]
    recycle_stopped: RecycleStop | None = None
    awaiting_respawn: list[AwaitingWorker] = Field(default_factory=list[AwaitingWorker])
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
