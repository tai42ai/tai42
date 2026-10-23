"""In-process (single-worker) implementation of the hooks manager."""

import heapq
import json
import logging
import time
from typing import Any, Literal

from tai42_contract.hooks.models import HookParams, TopicVerifierBinding
from tai42_contract.interactions import VisitAct

from tai42_skeleton.hooks.managers.base_hooks_manager import BaseHooksManager
from tai42_skeleton.hooks.runs.models import (
    ClaimIntent,
    HookRun,
    HookRunActed,
    HookRunCounts,
    HookRunsPage,
    ReplayClaim,
    RetryResult,
    StartSpec,
    page_offset,
)
from tai42_skeleton.hooks.settings import HooksSettings

logger = logging.getLogger(__name__)


class InMemoryHooksManager(BaseHooksManager):
    """A single-process hooks manager — registrations, the webhook seen-set and the hook runs live in memory."""

    def __init__(self, settings: HooksSettings):
        """Bind ``settings`` and initialise the in-memory hook, verifier, and webhook seen-set maps."""
        super().__init__(settings)
        self._hooks: dict[str, dict[str, HookParams]] = {}
        self._name_topic_map: dict[str, str] = {}
        self._topic_verifiers: dict[str, TopicVerifierBinding] = {}
        # Full seen-set key -> monotonic expiry. Per-process, matching this manager's
        # single-worker validity (siblings do not share it, exactly as they do not
        # share registrations here); the Redis manager holds the cross-worker seen-set.
        self._webhook_seen: dict[str, float] = {}
        # A min-heap of ``(expiry, key)`` mirroring ``_webhook_seen`` in expiry order, so
        # a claim pops only the already-lapsed prefix instead of scanning every id. TTLs
        # vary per claim, so expiry order is NOT insertion order — hence a heap, not a
        # queue. A key holds one live heap entry at a time (a re-claim happens only after
        # its prior entry lapses and is popped), so the heap stays bounded by the live
        # window, not history.
        self._webhook_seen_expiry: list[tuple[float, str]] = []
        # Hook-run jobs as the same flat string fields the Redis hash holds, so both backends
        # read a job back through one parser. ``_due`` is the current due score of every
        # pending and running job; ``_due_heap`` holds ``(score, id)`` in score order, an entry
        # whose score is no longer the job's current one being skipped when popped.
        self._runs: dict[str, dict[str, str]] = {}
        self._due: dict[str, float] = {}
        self._due_heap: list[tuple[float, str]] = []
        self._failed: dict[str, float] = {}

    @property
    def hook_count(self) -> int:
        """Number of live in-memory hooks across every topic bucket.

        A synchronous read (no async API needed) so the settings-reset hook can name
        how many hooks a config reload is about to drop from this manager.
        """
        return sum(len(bucket) for bucket in self._hooks.values())

    async def _store_hook(self, params: HookParams) -> bool:
        """Store a hook by name, moving it off any prior topic's bucket; returns ``True``."""
        key = self.settings.get_hook_key(params.topic)

        prev_topic = self._name_topic_map.get(params.name)
        if prev_topic is not None and prev_topic != params.topic:
            # Re-registering under a new topic: drop the entry from the old
            # topic's bucket, or the stale hook keeps firing there forever.
            prev_key = self.settings.get_hook_key(prev_topic)
            bucket = self._hooks.get(prev_key)
            if bucket is not None:
                bucket.pop(params.name, None)
                if not bucket:
                    del self._hooks[prev_key]

        self._hooks.setdefault(key, {})[params.name] = params
        self._name_topic_map[params.name] = params.topic
        return True

    async def unregister(self, name: str) -> bool:
        """Remove the hook named ``name``; returns ``True`` when one was present."""
        topic = self._name_topic_map.pop(name, None)
        if not topic:
            return False

        key = self.settings.get_hook_key(topic)

        if key in self._hooks and name in self._hooks[key]:
            del self._hooks[key][name]
            if not self._hooks[key]:
                del self._hooks[key]
        return True

    async def list_hooks_by_topic(self, topic: str) -> dict[str, HookParams]:
        """A copy of the hooks registered under ``topic``, keyed by name."""
        key = self.settings.get_hook_key(topic)
        # Return a copy, not the live bucket: ``on_event`` iterates this map
        # across an await (condition render), so a concurrent register/unregister
        # mutating the live dict mid-iteration would raise "dictionary changed
        # size during iteration".
        return dict(self._hooks.get(key, {}))

    async def list_hooks(self) -> dict[str, HookParams]:
        """Every registered hook across all topics, keyed by name."""
        all_hooks: dict[str, HookParams] = {}
        for hooks in self._hooks.values():
            all_hooks.update(hooks)
        return all_hooks

    async def set_topic_verifier(self, topic: str, binding: dict[str, Any]) -> None:
        """Store the verifier ``binding`` for ``topic``, validating its shape on write."""
        # Validate the shape on write (loud on a wrong shape) so both backends
        # enforce the same binding contract; store the canonical dict.
        self._topic_verifiers[topic] = TopicVerifierBinding.model_validate(binding)

    async def get_topic_verifier(self, topic: str) -> TopicVerifierBinding | None:
        """``topic``'s verifier binding (a frozen model), or ``None`` when none is set."""
        return self._topic_verifiers.get(topic)

    async def delete_topic_verifier(self, topic: str) -> bool:
        """Delete ``topic``'s verifier binding; returns ``True`` when one was present."""
        return self._topic_verifiers.pop(topic, None) is not None

    async def all_topic_verifiers(self) -> dict[str, TopicVerifierBinding]:
        """Every topic's verifier binding (frozen models), keyed by topic."""
        return dict(self._topic_verifiers)

    def _seen_key(self, topic: str, replay_claim: ReplayClaim) -> str:
        """The delivery's seen key, after dropping every lapsed claim; check it in ``_webhook_seen``."""
        now = time.monotonic()
        # Pop only the already-lapsed prefix off the expiry heap, not the whole set.
        # A popped entry is stale by construction; drop its id iff the id's CURRENT
        # expiry is also lapsed (a re-claim after a lapse holds a fresh, later expiry
        # whose own heap entry is untouched here).
        while self._webhook_seen_expiry and self._webhook_seen_expiry[0][0] <= now:
            _, lapsed_key = heapq.heappop(self._webhook_seen_expiry)
            current_expiry = self._webhook_seen.get(lapsed_key)
            if current_expiry is not None and current_expiry <= now:
                del self._webhook_seen[lapsed_key]
        return self.settings.webhook_seen_key(topic, replay_claim.key)

    def _mark_seen(self, key: str, replay_claim: ReplayClaim) -> None:
        """Claim the delivery's seen key for its replay window."""
        expiry = time.monotonic() + replay_claim.ttl_seconds
        self._webhook_seen[key] = expiry
        heapq.heappush(self._webhook_seen_expiry, (expiry, key))

    # -- Hook runs -------------------------------------------------------------
    #
    # Each method body runs with no ``await`` inside, so every step is atomic under the
    # single-threaded loop, the in-memory analogue of the Redis scripts.

    @property
    def run_counts(self) -> dict[str, int]:
        """How many jobs this manager holds per status, for the settings-reset warning."""
        counts = {"pending": 0, "running": 0, "failed": 0}
        for fields in self._runs.values():
            counts[fields["status"]] += 1
        return counts

    def _index_due(self, run_id: str, score: float) -> None:
        self._due[run_id] = score
        heapq.heappush(self._due_heap, (score, run_id))

    def _unindex_due(self, run_id: str) -> None:
        self._due.pop(run_id, None)

    def _held(self, run_id: str, claimed_by: str, op: str) -> dict[str, str] | None:
        fields = self._runs.get(run_id)
        if fields is None or fields.get("status") != "running" or fields.get("claimed_by") != claimed_by:
            logger.warning("hook run %s: this process no longer holds its lease, so it did not %s", run_id, op)
            return None
        return fields

    async def _accept_runs(
        self,
        topic: str,
        jobs: list[HookRun],
        replay_claim: ReplayClaim | None,
        claimed_by: str | None,
        lease_until: float | None,
    ) -> bool:
        """Write ``jobs`` and take ``replay_claim`` in one synchronous step; False when already seen.

        The claim is taken last, so a write that raises leaves the delivery unclaimed and a
        redelivery is accepted afresh.
        """
        seen_key = None
        if replay_claim is not None:
            seen_key = self._seen_key(topic, replay_claim)
            if seen_key in self._webhook_seen:
                return False
        written = [(job, job.to_fields()) for job in jobs]
        for job, fields in written:
            self._runs[job.id] = fields
            self._index_due(
                job.id, lease_until if claimed_by is not None and lease_until is not None else job.next_attempt_at
            )
        if seen_key is not None and replay_claim is not None:
            self._mark_seen(seen_key, replay_claim)
        return True

    async def _claim_due_runs(
        self, now: float, limit: int, claimed_by: str, lease_until: float, exclude: frozenset[str]
    ) -> list[HookRun]:
        """Claim up to ``limit`` due jobs, the earliest due first."""
        claimed: list[HookRun] = []
        skipped: list[tuple[float, str]] = []
        while self._due_heap and len(claimed) < limit and self._due_heap[0][0] <= now:
            score, run_id = heapq.heappop(self._due_heap)
            if self._due.get(run_id) != score:
                continue
            if run_id in exclude:
                skipped.append((score, run_id))
                continue
            fields = self._runs.get(run_id)
            if fields is None or fields["status"] not in ("pending", "running"):
                self._unindex_due(run_id)
                continue
            prior = fields["status"]
            fields.update(
                status="running",
                claimed_by=claimed_by,
                lease_until=repr(lease_until),
                attempts=str(int(fields["attempts"]) + 1),
            )
            self._index_due(run_id, lease_until)
            claimed.append(HookRun.from_fields(dict(fields), reclaimed=prior == "running"))
        for entry in skipped:
            heapq.heappush(self._due_heap, entry)
        return claimed

    async def _renew_lease(self, run_id: str, claimed_by: str, lease_until: float) -> bool:
        """Extend the lease of a job this claimer holds."""
        fields = self._held(run_id, claimed_by, "renew its lease")
        if fields is None:
            return False
        fields["lease_until"] = repr(lease_until)
        self._index_due(run_id, lease_until)
        return True

    async def _mark_acted(self, run_id: str, claimed_by: str, act: VisitAct, start_spec: StartSpec | None) -> bool:
        """Record a visit notice on the job."""
        fields = self._held(run_id, claimed_by, f"record its {act.action} {act.phase} notice")
        if fields is None:
            return False
        if act.phase == "before" and act.action in ("resume", "take"):
            fields["claim_intent"] = ClaimIntent(action=act.action, ids=act.interaction_ids).to_json()
            return True
        fields["acted"] = act.action
        if act.interaction_ids:
            ids = json.loads(fields["acted_ids"]) if "acted_ids" in fields else []
            fields["acted_ids"] = json.dumps([*ids, *act.interaction_ids])
        if act.phase == "committed":
            fields.pop("claim_intent", None)
        elif start_spec is not None:
            fields["start"] = start_spec.to_json()
        return True

    async def _clear_intent(self, run_id: str, claimed_by: str) -> bool:
        """Clear the job's claim intent."""
        fields = self._held(run_id, claimed_by, "clear its claim intent")
        if fields is None:
            return False
        fields.pop("claim_intent", None)
        return True

    async def _finish_run(self, run_id: str, claimed_by: str) -> bool:
        """Delete a job that succeeded."""
        if self._held(run_id, claimed_by, "record its success") is None:
            return False
        del self._runs[run_id]
        self._unindex_due(run_id)
        return True

    async def _reschedule_run(
        self, run_id: str, claimed_by: str, next_attempt_at: float, error: str, kind: str
    ) -> bool:
        """Set the job pending, due at ``next_attempt_at``."""
        fields = self._held(run_id, claimed_by, "schedule its retry")
        if fields is None:
            return False
        fields.update(status="pending", next_attempt_at=repr(next_attempt_at), last_error=error, last_error_kind=kind)
        fields.pop("claimed_by", None)
        fields.pop("lease_until", None)
        self._index_due(run_id, next_attempt_at)
        return True

    async def _fail_run(
        self,
        run_id: str,
        claimed_by: str,
        error: str,
        kind: str,
        failed_at: float,
        acted: HookRunActed,
        acted_ids: tuple[str, ...],
        claim_intent: ClaimIntent | None,
    ) -> HookRun | None:
        """Hold the job as failed; return it, or ``None`` when the lease was lost."""
        fields = self._held(run_id, claimed_by, "record its failure")
        if fields is None:
            return None
        fields.update(status="failed", last_error=error, last_error_kind=kind, failed_at=repr(failed_at), acted=acted)
        for name, value in (
            ("acted_ids", json.dumps(list(acted_ids)) if acted_ids else None),
            ("claim_intent", None if claim_intent is None else claim_intent.to_json()),
            ("claimed_by", None),
            ("lease_until", None),
        ):
            if value is None:
                fields.pop(name, None)
            else:
                fields[name] = value
        self._unindex_due(run_id)
        self._failed[run_id] = failed_at
        return HookRun.from_fields(dict(fields))

    async def _release_run(self, run_id: str, claimed_by: str, now: float) -> bool:
        """Set the job pending and due now."""
        fields = self._held(run_id, claimed_by, "release it")
        if fields is None:
            return False
        fields.update(status="pending", next_attempt_at=repr(now))
        fields.pop("claimed_by", None)
        fields.pop("lease_until", None)
        self._index_due(run_id, now)
        return True

    async def list_hook_runs(
        self, status: Literal["outstanding", "failed"] | None, limit: int, cursor: str | None
    ) -> HookRunsPage:
        """One page of hook runs: outstanding ones soonest due first, then failed ones most recent first."""
        offset = page_offset(cursor)
        ordered: list[str] = []
        if status in (None, "outstanding"):
            ordered += sorted(self._due, key=lambda run_id: (self._due[run_id], run_id))
        if status in (None, "failed"):
            ordered += sorted(self._failed, key=lambda run_id: (-self._failed[run_id], run_id))
        ids = ordered[offset : offset + limit]
        items = tuple(HookRun.from_fields(dict(self._runs[run_id])) for run_id in ids)
        return HookRunsPage(items=items, next_cursor=str(offset + limit) if len(ids) == limit else None)

    async def count_hook_runs(self) -> HookRunCounts:
        """How many hook runs are outstanding and how many are failed."""
        return HookRunCounts(outstanding=len(self._due), failed=len(self._failed))

    async def retry_hook_run(
        self, run_id: str, now: float, *, claimed_by: str | None = None, lease_until: float | None = None
    ) -> RetryResult:
        """Re-queue a failed job as a fresh run."""
        fields = self._runs.get(run_id)
        if fields is None:
            return RetryResult(outcome="missing")
        if fields["status"] != "failed":
            return RetryResult(outcome="not_failed")
        if fields.get("acted") in ("resume", "take"):
            return RetryResult(outcome="refused", run=HookRun.from_fields(dict(fields)))
        if fields.get("acted") != "start":
            fields["acted"] = ""
            fields.pop("acted_ids", None)
            fields.pop("start", None)
        fields.pop("claim_intent", None)
        fields.pop("failed_at", None)
        if claimed_by is not None and lease_until is not None:
            fields.update(
                status="running",
                attempts="1",
                next_attempt_at=repr(now),
                claimed_by=claimed_by,
                lease_until=repr(lease_until),
            )
            self._index_due(run_id, lease_until)
        else:
            fields.update(status="pending", attempts="0", next_attempt_at=repr(now))
            self._index_due(run_id, now)
        del self._failed[run_id]
        return RetryResult(outcome="ok", run=HookRun.from_fields(dict(fields)))

    async def discard_hook_run(self, run_id: str) -> Literal["ok", "missing", "not_failed"]:
        """Delete a failed job."""
        fields = self._runs.get(run_id)
        if fields is None:
            return "missing"
        if fields["status"] != "failed":
            return "not_failed"
        del self._runs[run_id]
        self._failed.pop(run_id, None)
        self._unindex_due(run_id)
        return "ok"
