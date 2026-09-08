"""Shared response models for the ``/api/*`` success surface.

The pydantic-only contract layer is the home for response shapes more than one
package (skeleton operations, premium plugins, the flows engine) declares on a
route, so a model shared across the seam lives here and is imported by full
submodule path (``from tai42_contract.app.responses import ...``). Each model
DESCRIBES the payload a route returns today, wrapped by the caller in the
``{"data": ...}`` success envelope — it never re-declares the envelope and never
reshapes a wire body.
"""

from __future__ import annotations

from pydantic import BaseModel, JsonValue, RootModel


class FanoutWorkerResult(BaseModel):
    """One worker's verdict within a multi-worker fan-out report.

    ``payload`` carries a query op's per-worker data; ``error`` a failed apply's
    message; ``detail`` the publisher's note for a computed missing/departed/
    timed-out verdict. ``outcome`` is the worker's terminal outcome as its wire
    string."""

    name: str
    outcome: str
    payload: JsonValue | None = None
    error: str | None = None
    detail: str | None = None


class FanoutSummary(BaseModel):
    """The mode-tagged fan-out summary embedded under a mutation response's
    ``fanout`` field.

    ``mode`` selects the shape: ``local-only`` carries only ``note`` (a lone
    worker reached no sibling); ``fleet`` and ``unreachable`` carry the per-worker
    broadcast report (``op``/``reachable``/``local_only``/``results``/``error``),
    the ``unreachable`` variant having no worker list, only ``error``. The
    mode-variant fields are optional so one model describes every mode without
    reshaping any wire payload."""

    mode: str
    note: str | None = None
    op: str | None = None
    reachable: bool | None = None
    local_only: bool | None = None
    results: list[FanoutWorkerResult] | None = None
    error: str | None = None


class ApplyResponse(BaseModel):
    """The standard mutation-op response: this worker's local reload result merged
    with the fleet fan-out summary. ``env_keys`` is the count of env keys the
    reload loaded."""

    status: str
    env_keys: int
    fanout: FanoutSummary


class RecycleEntry(BaseModel):
    """One recycled or self-deferred worker line in a profile-apply report."""

    name: str
    kind: str
    status: str
    generation_before: int


class FreshLife(BaseModel):
    """One newly-ready worker life observed since the pre-apply snapshot."""

    name: str
    kind: str
    generation: int


class ProfileApplyResponse(BaseModel):
    """The dedicated profile-apply response. ``hot`` are the hot-class diff key
    names; ``recycle`` the recycled/self-deferred worker lines; ``fresh`` the new
    ready lives seen since the pre-apply snapshot; ``refused`` is empty on success
    (any refusal aborts the pipeline before a response is built); ``fanout`` the
    reload broadcast's fleet summary. Names only — never env values."""

    hot: list[str]
    recycle: list[RecycleEntry]
    fresh: list[FreshLife]
    refused: list[str]
    fanout: FanoutSummary


class DeleteResult(BaseModel):
    """The recurring delete-confirmation shape. ``id`` and ``user_id`` are both
    optional so the one model covers every delete door's variant (``{deleted, id}``
    or ``{deleted, user_id}``) without a site reshaping its body."""

    deleted: bool
    id: str | None = None
    user_id: str | None = None


class OpaqueJson(RootModel[JsonValue]):
    """The deliberate "any JSON value" marker for a genuinely-open body — a tool or
    upstream passthrough whose shape is not fixed. A named subclass, never a bare
    alias: the emitter registers a component under ``model.__name__``, so only a
    real class yields a stable, unique component name. Reach for it ONLY for a
    genuinely-open body, never as a shortcut past authoring a real model."""
