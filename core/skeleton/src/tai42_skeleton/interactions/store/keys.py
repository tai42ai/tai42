"""The Redis key contract for the interactions store: one prefix and every key-shape method the ops share."""

from __future__ import annotations

from tai42_contract.conversation_target import ConversationTargetKind


def _seg(value: str) -> str:
    """Escape a free-form key segment so ``:`` never introduces a boundary ambiguity.

    A subject ``target_name`` or ``key`` is arbitrary text (a thread key is itself
    colon-joined, e.g. ``bridge:chat:+1555``), so joining the four subject fields with a
    raw ``:`` would let two distinct subjects collide onto one SET and cross-leak their
    members. Percent-escaping ``%`` then ``:`` in every free-form segment makes the join
    injective: distinct subjects always map to distinct keys. The key is only ever
    constructed from a known tuple and looked up by the exact string, never parsed back,
    so a one-way injective encoding is all it needs.
    """
    return value.replace("%", "%25").replace(":", "%3A")


class _StoreKeys:
    def __init__(self, key_prefix: str) -> None:
        self._p = key_prefix

    def group_key(self, group_id: str) -> str:
        return f"{self._p}group:{group_id}"

    def state_key(self, interaction_id: str) -> str:
        return f"{self._p}state:{interaction_id}"

    def reply_key(self, interaction_id: str) -> str:
        return f"{self._p}reply:{interaction_id}"

    def ticket_key(self, ticket: str) -> str:
        return f"{self._p}ticket:{ticket}"

    @property
    def pending_key(self) -> str:
        return f"{self._p}pending"

    @property
    def pending_deadline_key(self) -> str:
        """Parallel index to ``pending_key``, scored by each group's FURTHEST question deadline.

        Extend-only via ``ZADD GT``. ``pending_key`` is scored by creation TIME, not deadline — ``add`` sets
        a group's score to its most recent question's ``created_at`` — so the pending list reads in
        creation-timestamp order rather than deadline order; only this parallel index carries the deadline
        the atomic phantom purge keys on, and the purge never rescores ``pending_key``.
        """
        return f"{self._p}pending:deadline"

    @property
    def open_key(self) -> str:
        return f"{self._p}open"

    @property
    def pending_expiry_key(self) -> str:
        """Per-INTERACTION deadline index for async parks: member = interaction id, score = ``expiry_at`` in ms.

        Distinct from ``pending_deadline_key`` (group keyed, scored by sync ``timeout_at``, purged of
        expired GROUPS on every ``add``): an async park's continuation must fire exactly once at expiry, so
        it needs an interaction-level index the group phantom-purge never drops. The expiry reaper scans
        it; ``record_answer``/``prune_pending`` remove a member when the question leaves pending. Every
        async park carries an ``expiry_at`` and so is a member; a sync question has no expiry and is never
        indexed here.
        """
        return f"{self._p}pending:expiry"

    def continuation_due_key(self, interaction_id: str) -> str:
        """The durable continuation-due record for one async park.

        A self-contained, flow-blind hash (tool NAME, execution identity, key fingerprint, generic answer,
        attempt count) written when the park resolves and cleared when its continuation's ``run_tool``
        returns. Its presence past a healthy fire's window is the reaper's signal to redeliver.
        """
        return f"{self._p}continuation:due:{interaction_id}"

    @property
    def continuation_due_index_key(self) -> str:
        """Deadline index over the continuation-due records: member = interaction id, score = next-attempt ms.

        The redelivery reaper scans it for members due at or before now; a member is dropped when its
        continuation's ``run_tool`` returns (``clear_continuation_due``) or reconciled off when its record
        hash has TTL-expired.
        """
        return f"{self._p}continuation:due"

    @property
    def _count_prefix(self) -> str:
        return f"{self._p}pending:count:"

    def count_key(self, group_id: str) -> str:
        return f"{self._count_prefix}{group_id}"

    @property
    def events_key(self) -> str:
        return f"{self._p}events"

    def media_key(self, media_id: str) -> str:
        """The hash holding one stored-by-reference media item (its mime + base64 bytes).

        Keyed by the media id (the served-media capability secret); its TTL is set-or-extended to the
        owning group's horizon on every ``add`` (media of a group lives as long as the group).
        """
        return f"{self._p}media:{media_id}"

    def media_index_key(self, group_id: str) -> str:
        """SET of the stored-media ids a group's questions reference.

        ``add`` extends every member ``media_key`` and this index to the group's TTL, so the bytes a
        durable record points at outlive no shorter than the group stream.
        """
        return f"{self._p}media-index:{group_id}"

    def thread_parks_key(self, thread_id: str) -> str:
        """SET of the interaction ids of the async PARKS bound to one conversation thread.

        The reverse index a thread delete reads to cascade-cancel every parked ``ask`` the deletion
        would otherwise ORPHAN (its expiry reaper later firing a continuation into a thread that no longer
        exists, its channel correlation muted until the deadline). There is otherwise no thread→interaction
        edge: the state is keyed by interaction id alone.

        A member is written in the park's OWN ``add`` pipeline — only for an async park
        that carries a bound thread (a background tool run with no thread writes none) —
        so it commits atomically with the ``state``/``pending``/expiry writes. It is
        dropped wherever the interaction leaves pending (``record_answer`` and
        ``prune_pending``), keyed off the ``thread_id`` denormalized on the state hash.
        The set's TTL is set-or-extended to the park's own ``_key_ttl`` (the same NX+GT
        discipline the group stream uses), so it outlives the longest-lived park it
        indexes; drained empty it simply expires.
        """
        return f"{self._p}thread-parks:{thread_id}"

    def subject_parks_key(self, target_kind: ConversationTargetKind, target_name: str, kind: str, key: str) -> str:
        """SET of the parked-entry ids addressed to one subject ``(target_kind, target_name, kind, key)``.

        A subject is the state store's addressing unit: the ``(target_kind, target_name)`` scope
        plus a ``(kind, key)`` within it. Every async park that carries a state context joins this
        set — once per ``candidates.by_kind`` entry — under its own scope, user asks and caller
        asks alike; a waiting outcome joins it in the same way keyed by its delivery id. The set
        is the forward index :meth:`list_parked_for` unions and the reverse index a subject erasure
        and a person merge walk to reach every entry addressed to a subject WITHOUT a
        SCAN. Membership is dropped wherever the backing record leaves the store (prune, kill,
        outcome take).

        The four fields are joined with ``:``; the free-form ``target_name`` and ``key`` are
        escaped (:func:`_seg`) so a colon inside either can never blur a field boundary and merge
        two distinct subjects onto one set.
        """
        return f"{self._p}subject-parks:{target_kind}:{_seg(target_name)}:{kind}:{_seg(key)}"

    def subject_scopes_key(self, kind: str, key: str) -> str:
        """SET of the ``(target_kind, target_name)`` scopes that hold a park under one ``(kind, key)``.

        A subject erasure or a person merge knows a ``(kind, key)`` (a person id, a thread key) but
        not which conversation-target scopes carry parks under it; this set enumerates them so the
        walk reaches every :meth:`subject_parks_key` set without a SCAN. Each member is the scope
        token ``f"{target_kind}:{target_name}"`` — ``target_kind`` is a colon-free
        :data:`ConversationTargetKind`, so a reader splits it on the FIRST ``:`` to recover the
        pair whatever the ``target_name`` contains. A coarse index: a scope may linger after its
        last park leaves, which only yields an empty forward lookup on a later walk (never a
        wrong hit), so it is never pruned per-park — only re-keyed by the merge and expired by TTL.
        """
        return f"{self._p}subject-scopes:{kind}:{_seg(key)}"

    @property
    def open_caller_key(self) -> str:
        """The second open-slot ZSET: the concurrency index for ``to="caller"`` asks and waiting outcomes.

        Independent of :attr:`open_key` (which bounds ``to="user"`` asks under
        ``max_concurrent``), this set is bounded by ``max_concurrent_caller``. A caller ask
        reserves a member here at park time; a waiting outcome takes one when a run terminates
        (counted here but never refused — a finished run's result is never lost); a take, prune, or
        kill frees it. Member score is the entry's ``timeout_at`` in ms, so a SIGKILLed waiter's
        member is purged by the same ``ZREMRANGEBYSCORE`` the user index uses.
        """
        return f"{self._p}open:caller"

    def outcome_key(self, completion_id: str) -> str:
        """The durable WAITING-OUTCOME record for one resumed run's terminal, keyed by its delivery id.

        When a resumed run reaches a terminal with a subject but no live receiver and no address,
        the platform writes the terminal here (``finished``/``failed`` with the run's ``result`` or
        ``error``) so whoever owns the subject can take it later. Keyed by the per-run
        ``completion_id`` (``uuid5`` of the run's delivery identity), so a redelivery of the same
        run — or a buffered sibling re-driving the SAME terminal — writes the SAME key and the
        second write is a no-op: exactly one row per run terminal. Taken atomically
        (:meth:`~..writes._StoreWrites.claim_outcome`); dropped on kill/erase or by the retention
        sweep.
        """
        return f"{self._p}outcome:{completion_id}"

    @property
    def outcome_retention_index_key(self) -> str:
        """Retention index over the waiting outcomes: member = ``completion_id``, score = ``created_at_ms``.

        The retention sweep scans it for outcomes older than the retention horizon and drops each
        untaken one (emitting ``interactions_outcome_dropped_untaken``), so an aged outcome is never
        silently deleted by its Redis TTL backstop. A member is written in :meth:`add_outcome`'s MULTI
        and dropped when the outcome is taken (:meth:`claim_outcome`), killed/erased or swept
        (:meth:`delete_outcome`).
        """
        return f"{self._p}outcome:retention"

    def kill_due_key(self, interaction_id: str) -> str:
        """The durable KILL-DUE record for one whole-chain kill, mirroring the continuation-due outbox.

        Written in the kill's MULTI carrying the killed run's own copied ``delivery`` and
        ``run_delivery_id`` (self-contained, so the run's FAILED delivery and its dedup id survive
        the prune and a redelivery). It is cleared only when the driver teardown returned normally
        AND the platform's FAILED delivery for the killed run committed; while it stands the reaper
        redelivers the kill, and past its retention horizon it is dropped with a loud give-up.
        """
        return f"{self._p}kill:due:{interaction_id}"

    @property
    def kill_due_index_key(self) -> str:
        """Deadline index over the kill-due records: member = interaction id, score = next-attempt ms.

        The kill-due reaper leg scans it for members due at or before now, exactly as the
        continuation-due reaper scans :attr:`continuation_due_index_key`; a member is dropped when
        the kill's teardown-and-delivery completes or its record TTL-expires past the horizon.
        """
        return f"{self._p}kill:due"
