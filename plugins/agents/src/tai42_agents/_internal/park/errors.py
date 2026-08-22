"""Loud, named failures on the agent async-park resume path.

Every resume fault raises one of these rather than returning a benign shape: the
platform's continuation seam leaves the durable due-record in place on a raise, so the
expiry reaper redelivers until the resume completes. A silent return would clear the
record and strand the park.
"""

from __future__ import annotations

from datetime import datetime


class AgentResumeParkEntryNotFoundError(RuntimeError):
    """No park entry exists for the interaction being resumed — never parked here,
    already resumed, or its thread ended."""

    def __init__(self, interaction_id: str) -> None:
        self.interaction_id = interaction_id
        super().__init__(f"no agent park entry for interaction {interaction_id!r}")


class AgentResumeBarrierNotFoundError(RuntimeError):
    """The super-step barrier the answer converges on is gone (expired or cleared) while
    a park entry still points at it."""

    def __init__(self, thread_id: str, superstep_id: str) -> None:
        self.thread_id = thread_id
        self.superstep_id = superstep_id
        super().__init__(f"no agent park barrier for thread {thread_id!r} super-step {superstep_id!r}")


class AgentResumeInterruptNotPendingError(RuntimeError):
    """The interrupt an answer targets is not (or no longer) pending on the parked graph
    — a corrupted routing, or a graph that already advanced past it."""

    def __init__(self, interaction_id: str, interrupt_id: str) -> None:
        self.interaction_id = interaction_id
        self.interrupt_id = interrupt_id
        super().__init__(f"agent park interrupt {interrupt_id!r} for interaction {interaction_id!r} is not pending")


class AgentResumeDriveInProgressError(RuntimeError):
    """The barrier is complete but another live worker already holds the drive lease.

    Raised (never a benign return) so the platform keeps this continuation's durable
    retry ticket — the reaper redelivers until the live drive completes or its lease
    expires."""

    def __init__(self, thread_id: str, superstep_id: str) -> None:
        self.thread_id = thread_id
        self.superstep_id = superstep_id
        super().__init__(f"agent resume drive already in progress for thread {thread_id!r} super-step {superstep_id!r}")


class AgentParkMarkerError(RuntimeError):
    """A parked ToolMessage could not be reconciled with the resume answers — a marked
    message missing on replay, or a marker whose interaction id has no answer. Raised so a
    resume never silently substitutes a partial or wrong answer."""


class WorkspaceLeaseHeldError(RuntimeError):
    """Another worker holds the per-workspace drive lease for a threaded run.

    A durable-workspace agent (``claude_code`` / ``langchain_deep_agent``) serializes the
    turns of one ``thread_id`` across workers on a shared Redis lease — the workspace volume
    is NOT idempotent under concurrent drives (two sessions writing one volume corrupt each
    other). Raised (constant message, never the workspace path) when ``SET NX`` finds the
    lease already held, so a second concurrent turn busy-errors loudly rather than starting a
    corrupting second session."""

    def __init__(self, workspace_key: str) -> None:
        self.workspace_key = workspace_key
        super().__init__("another worker is already driving this agent workspace; retry after it releases")


class ParkExpiryExceedsRetentionError(RuntimeError):
    """A parked ask's expiry deadline outlives the run's retention horizon.

    A parked run lives only as long as EVERY store backing it — its LangGraph checkpoint,
    and (for a durable-workspace engine) its sandbox volume. The caller-computed retention
    bound is the nearest such horizon; under a bounded one, a deadline beyond it — or a park
    with NO deadline at all — would let a backing store be swept before the ask resolves,
    leaving an unresumable park. Raised at park-persist time so the whole super-step fails
    with zero park-index state written, never a park that silently cannot be resumed."""

    def __init__(self, interaction_id: str, expiry_at: str | None, horizon: datetime) -> None:
        self.interaction_id = interaction_id
        self.expiry_at = expiry_at
        self.horizon = horizon
        deadline = "no expiry deadline" if expiry_at is None else f"expiry_at {expiry_at!r}"
        super().__init__(
            f"parked interaction {interaction_id!r} has {deadline}, which exceeds the run's "
            f"retention horizon {horizon.isoformat()}: a backing store would be swept before the "
            "ask resolves"
        )
