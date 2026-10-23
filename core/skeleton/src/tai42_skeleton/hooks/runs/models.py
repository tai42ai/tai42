"""The hook-run job and the values the accept, the attempts and the operator doors pass around.

A job is stored as one flat string hash (Redis) or as the same mapping in memory, so every value
below has an exact string form: ``to_fields`` writes it and ``from_fields`` reads it back. A field
that is absent is not written at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from tai42_contract.states import StateBinding, StateContext

HookRunStatus = Literal["pending", "running", "failed"]
HookRunActed = Literal["", "cancel", "resume", "take", "start"]
ClaimAction = Literal["resume", "take"]

_ACTED: frozenset[str] = frozenset({"", "cancel", "resume", "take", "start"})
_STATUSES: frozenset[str] = frozenset({"pending", "running", "failed"})


@dataclass(frozen=True, slots=True)
class ReplayClaim:
    """A webhook delivery id to claim together with keeping the event's runs.

    ``key`` is the verifier's per-delivery id and ``ttl_seconds`` the window in which the same id
    is answered ``already_seen``. The claim is never taken without a bounded window.
    """

    key: str
    ttl_seconds: int

    def __post_init__(self) -> None:
        """Refuse a claim without a positive window."""
        if self.ttl_seconds <= 0:
            raise ValueError(f"webhook replay claim requires a positive ttl_seconds, got {self.ttl_seconds!r}")


@dataclass(frozen=True, slots=True)
class DispatchKey:
    """The trigger link's execution key and its per-mint fingerprint, bound around every attempt."""

    execution_key: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class AcceptResult:
    """What an accept did: ``accepted`` with the kept run ids, or ``already_seen`` with none."""

    status: Literal["accepted", "already_seen"]
    run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StartSpec:
    """The start a hook's visit made, kept so the start alone can be replayed.

    ``input`` is the merged tool input, ``extras`` the visit extras without the idempotency key,
    ``state_context`` the hook's state context and ``state_binding`` the hook's door binding.
    """

    tool: str
    input: dict[str, Any]
    extras: dict[str, Any]
    state_context: StateContext | None
    state_binding: StateBinding | None

    def to_json(self) -> str:
        """The spec's JSON form."""
        return json.dumps(
            {
                "tool": self.tool,
                "input": self.input,
                "extras": self.extras,
                "state_context": None if self.state_context is None else self.state_context.model_dump(mode="json"),
                "state_binding": None if self.state_binding is None else self.state_binding.model_dump(mode="json"),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> StartSpec:
        """Read a spec back from its JSON form."""
        data = json.loads(raw)
        return cls(
            tool=data["tool"],
            input=data["input"],
            extras=data["extras"],
            state_context=None if data["state_context"] is None else StateContext.model_validate(data["state_context"]),
            state_binding=None if data["state_binding"] is None else StateBinding.model_validate(data["state_binding"]),
        )


@dataclass(frozen=True, slots=True)
class ClaimIntent:
    """A resume or take claim a hook's visit announced and has not yet confirmed as committed."""

    action: ClaimAction
    ids: tuple[str, ...]

    def to_json(self) -> str:
        """The intent's JSON form."""
        return json.dumps({"action": self.action, "ids": list(self.ids)})

    @classmethod
    def from_json(cls, raw: str) -> ClaimIntent:
        """Read an intent back from its JSON form."""
        data = json.loads(raw)
        action = data["action"]
        if action not in ("resume", "take"):
            raise ValueError(f"a stored claim intent names an unknown action {action!r}")
        return cls(action=action, ids=tuple(data["ids"]))


@dataclass(frozen=True, slots=True)
class HookRun:
    """One hook-run job: one hook's run of one accepted event.

    ``reclaimed`` is not stored: a claim sets it when the job it claimed was ``running`` (a lease
    that lapsed), so the attempt applies the interrupted-attempt rule first.
    """

    id: str
    topic: str
    hook: str
    payload: dict[str, Any]
    status: HookRunStatus
    attempts: int
    next_attempt_at: float
    created_at: float
    tool_kwargs_override: dict[str, Any] | None = None
    dispatch_key: DispatchKey | None = None
    monitoring_disabled: bool = False
    claimed_by: str | None = None
    lease_until: float | None = None
    acted: HookRunActed = ""
    acted_ids: tuple[str, ...] = ()
    claim_intent: ClaimIntent | None = None
    start: StartSpec | None = None
    last_error: str | None = None
    last_error_kind: str | None = None
    failed_at: float | None = None
    reclaimed: bool = field(default=False, compare=False)

    def to_fields(self) -> dict[str, str]:
        """The job's hash fields; an absent value is left out."""
        fields: dict[str, str] = {
            "id": self.id,
            "topic": self.topic,
            "hook": self.hook,
            "payload": json.dumps(self.payload),
            "status": self.status,
            "attempts": str(self.attempts),
            "next_attempt_at": repr(self.next_attempt_at),
            "created_at": repr(self.created_at),
            "acted": self.acted,
        }
        optional: dict[str, str | None] = {
            "tool_kwargs_override": (
                None if self.tool_kwargs_override is None else json.dumps(self.tool_kwargs_override)
            ),
            "dispatch_key": None if self.dispatch_key is None else self.dispatch_key.execution_key,
            "dispatch_fingerprint": None if self.dispatch_key is None else self.dispatch_key.fingerprint,
            "monitoring_disabled": "1" if self.monitoring_disabled else None,
            "claimed_by": self.claimed_by,
            "lease_until": None if self.lease_until is None else repr(self.lease_until),
            "acted_ids": json.dumps(list(self.acted_ids)) if self.acted_ids else None,
            "claim_intent": None if self.claim_intent is None else self.claim_intent.to_json(),
            "start": None if self.start is None else self.start.to_json(),
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "failed_at": None if self.failed_at is None else repr(self.failed_at),
        }
        fields.update({name: value for name, value in optional.items() if value is not None})
        return fields

    @classmethod
    def from_fields(cls, fields: dict[str, str], *, reclaimed: bool = False) -> HookRun:
        """Read a job back from its hash fields; a malformed field raises."""
        status = fields["status"]
        if status not in _STATUSES:
            raise ValueError(f"hook run {fields.get('id')!r} carries an unknown status {status!r}")
        acted = fields.get("acted", "")
        if acted not in _ACTED:
            raise ValueError(f"hook run {fields.get('id')!r} carries an unknown acted marker {acted!r}")
        dispatch_key = (
            DispatchKey(fields["dispatch_key"], fields["dispatch_fingerprint"]) if "dispatch_key" in fields else None
        )
        return cls(
            id=fields["id"],
            topic=fields["topic"],
            hook=fields["hook"],
            payload=json.loads(fields["payload"]),
            status=cast("HookRunStatus", status),
            attempts=int(fields["attempts"]),
            next_attempt_at=float(fields["next_attempt_at"]),
            created_at=float(fields["created_at"]),
            tool_kwargs_override=(
                json.loads(fields["tool_kwargs_override"]) if "tool_kwargs_override" in fields else None
            ),
            dispatch_key=dispatch_key,
            monitoring_disabled=fields.get("monitoring_disabled") == "1",
            claimed_by=fields.get("claimed_by"),
            lease_until=float(fields["lease_until"]) if "lease_until" in fields else None,
            acted=cast("HookRunActed", acted),
            acted_ids=tuple(json.loads(fields["acted_ids"])) if "acted_ids" in fields else (),
            claim_intent=ClaimIntent.from_json(fields["claim_intent"]) if "claim_intent" in fields else None,
            start=StartSpec.from_json(fields["start"]) if "start" in fields else None,
            last_error=fields.get("last_error"),
            last_error_kind=fields.get("last_error_kind"),
            failed_at=float(fields["failed_at"]) if "failed_at" in fields else None,
            reclaimed=reclaimed,
        )


@dataclass(frozen=True, slots=True)
class HookRunCounts:
    """How many hook runs are outstanding (pending or running) and how many are failed."""

    outstanding: int
    failed: int


@dataclass(frozen=True, slots=True)
class HookRunsPage:
    """One page of hook runs, with the cursor of the next page.

    Outstanding runs come first, the soonest due first; failed runs follow, the most recently
    failed first.
    """

    items: tuple[HookRun, ...]
    next_cursor: str | None


def page_offset(cursor: str | None) -> int:
    """The offset a hook-runs page cursor names; a cursor that is not a non-negative integer raises."""
    if cursor is None:
        return 0
    if not cursor.isdigit():
        raise ValueError(f"a hook-runs cursor is a non-negative integer, got {cursor!r}")
    return int(cursor)


RetryOutcome = Literal["ok", "missing", "not_failed", "refused"]


@dataclass(frozen=True, slots=True)
class RetryResult:
    """What an operator retry found: ``ok`` with the re-queued job, or why it did nothing.

    ``refused`` carries the failed job, so the caller names what its hook committed.
    """

    outcome: RetryOutcome
    run: HookRun | None = None


__all__ = [
    "AcceptResult",
    "ClaimAction",
    "ClaimIntent",
    "DispatchKey",
    "HookRun",
    "HookRunActed",
    "HookRunCounts",
    "HookRunStatus",
    "HookRunsPage",
    "ReplayClaim",
    "RetryOutcome",
    "RetryResult",
    "StartSpec",
    "page_offset",
]
