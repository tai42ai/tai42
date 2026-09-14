"""The Redis key contract for the interactions store: one prefix and every
key-shape method the read and write operations share."""

from __future__ import annotations


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
        """Parallel index to ``pending_key``, scored by each group's FURTHEST
        question deadline (extend-only via ``ZADD GT``). ``pending_key`` is scored
        by creation TIME, not deadline — ``add`` sets a group's score to its most
        recent question's ``created_at`` — so the pending list reads in
        creation-timestamp order rather than deadline order; only this parallel
        index carries the deadline the atomic phantom purge keys on, and the purge
        never rescores ``pending_key``."""
        return f"{self._p}pending:deadline"

    @property
    def open_key(self) -> str:
        return f"{self._p}open"

    @property
    def pending_expiry_key(self) -> str:
        """Per-INTERACTION deadline index for async parks: member = interaction id,
        score = ``expiry_at`` in ms. Distinct from ``pending_deadline_key`` (group
        keyed, scored by sync ``timeout_at``, purged of expired GROUPS on every
        ``add``): an async park's continuation must fire exactly once at expiry, so
        it needs an interaction-level index the group phantom-purge never drops.
        The expiry reaper scans it; ``record_answer``/``prune_pending`` remove a
        member when the question leaves pending. Every async park carries an
        ``expiry_at`` and so is a member; a sync question has no expiry and is never
        indexed here."""
        return f"{self._p}pending:expiry"

    def continuation_due_key(self, interaction_id: str) -> str:
        """The durable continuation-due record for one async park: a self-contained,
        flow-blind hash (tool NAME, execution identity, key fingerprint, generic
        answer, attempt count) written when the park resolves and cleared when its
        continuation's ``run_tool`` returns. Its presence past a healthy fire's
        window is the reaper's signal to redeliver."""
        return f"{self._p}continuation:due:{interaction_id}"

    @property
    def continuation_due_index_key(self) -> str:
        """Deadline index over the continuation-due records: member = interaction id,
        score = the next-attempt time in ms. The redelivery reaper scans it for
        members due at or before now; a member is dropped when its continuation's
        ``run_tool`` returns (``clear_continuation_due``) or reconciled off when its
        record hash has TTL-expired."""
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
        """The hash holding one stored-by-reference media item (its mime + base64
        bytes). Keyed by the media id (the served-media capability secret); its TTL
        is set-or-extended to the owning group's horizon on every ``add`` (media of a
        group lives as long as the group)."""
        return f"{self._p}media:{media_id}"

    def media_index_key(self, group_id: str) -> str:
        """SET of the stored-media ids a group's questions reference. ``add`` extends
        every member ``media_key`` and this index to the group's TTL, so the bytes a
        durable record points at outlive no shorter than the group stream."""
        return f"{self._p}media-index:{group_id}"

    def thread_parks_key(self, thread_id: str) -> str:
        """SET of the interaction ids of the async PARKS bound to one conversation
        thread — the reverse index a thread delete reads to cascade-cancel every
        parked ``ask_user`` the deletion would otherwise ORPHAN (its expiry reaper
        later firing a continuation into a thread that no longer exists, its channel
        correlation muted until the deadline). There is otherwise no thread→interaction
        edge: the state is keyed by interaction id alone.

        A member is written in the park's OWN ``add`` pipeline — only for an async park
        that carries a bound thread (a background tool run with no thread writes none) —
        so it commits atomically with the ``state``/``pending``/expiry writes. It is
        dropped wherever the interaction leaves pending (``record_answer`` and
        ``prune_pending``), keyed off the ``thread_id`` denormalized on the state hash.
        The set's TTL is set-or-extended to the park's own ``_key_ttl`` (the same NX+GT
        discipline the group stream uses), so it outlives the longest-lived park it
        indexes; drained empty it simply expires."""
        return f"{self._p}thread-parks:{thread_id}"
