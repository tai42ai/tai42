"""The conversation bridge's Redis keyspace layout: every key the store reads and writes, built from one prefix.

Every literal key string lives ONLY here. A provider-supplied id sits LAST in its key and the segment
before it is checked ``:``-free, so no provider value can bleed across a segment boundary — the sole
exceptions are ``message_ref_key``, ``open_code_key`` and the two redeem-throttle keys, whose variable
part is a single opaque ``sha256`` terminal segment folding charset-unconstrained inputs into a hash,
so they carry no qualifier guard.

The person keyspaces beside the person row hold its erase mark, the ids it absorbed, its thread's
recorded routes and an absorbed id's absorbed-into pointer; each writer's threads list the route-keyed
threads it wrote in, so a person's erase visits them and wipes those only that person and the ids it
absorbed wrote in.
"""

import hashlib
import json
from dataclasses import dataclass

from tai42_skeleton.conversations.thread_ids import BRIDGE_THREAD_PREFIX

__all__ = ["ConversationsKeyspace"]


def _require_key_segment(name: str, value: str) -> None:
    """Raise on a blank key segment.

    A blank segment builds a well-formed key every other blank value also builds, silently colliding
    distinct messages onto one marker/index entry.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string to key a conversation record, got {value!r}")


def _require_qualifier_segment(name: str, value: str) -> None:
    """Raise on a blank or ``:``-bearing key segment that another segment FOLLOWS.

    A ``:`` moves the boundary, so ``(a, b:c)`` and ``(a:b, c)`` build the same key. A trailing segment
    needs no such check.
    """
    _require_key_segment(name, value)
    if ":" in value:
        raise ValueError(f"{name} must not contain ':' — it would collapse into the segment that follows it: {value!r}")


@dataclass(frozen=True, slots=True)
class ConversationsKeyspace:
    """The conversation keys under one ``prefix`` (``CONVERSATIONS_PREFIX``).

    Reached as :attr:`~tai42_skeleton.conversations.settings.ConversationsSettings.keyspace`.
    """

    prefix: str

    def dedupe_key(self, channel: str, provider_message_id: str) -> str:
        """Inbound-dedupe marker key, channel-qualified (a provider message id is unique only within its channel).

        Both halves must be non-blank.
        """
        _require_qualifier_segment("channel", channel)
        _require_key_segment("provider_message_id", provider_message_id)
        return f"{self.prefix}:dedupe:{channel}:{provider_message_id}"

    def event_dedupe_key(self, route_name: str, event_id: str) -> str:
        """Event-dedupe marker key, route-qualified (an event id is unique only within its route).

        Its OWN family, distinct from :meth:`dedupe_key`'s channel namespace, so a channel provider id and
        an event id sharing a value never collide on one marker; the same ``inbound_dedupe_ttl_seconds``
        governs both. Both halves must be non-blank.
        """
        _require_qualifier_segment("route_name", route_name)
        _require_key_segment("event_id", event_id)
        return f"{self.prefix}:event-dedupe:{route_name}:{event_id}"

    def record_key(self, message_id: str) -> str:
        """Answer/delivery record key, keyed by the record's uuid4 ``message_id``."""
        return f"{self.record_key_prefix}{message_id}"

    @property
    def record_key_prefix(self) -> str:
        """The record key prefix the writer scripts build a record key from in-script."""
        return f"{self.prefix}:record:"

    def status_index_key(self, delivery_status: str) -> str:
        """Per-status record index — the sorted set of the ``message_id``s in that state.

        Read by the re-drive and the sweep instead of walking the record keyspace. A member's score is the
        moment its row expires (``+inf`` while it carries no TTL).
        """
        return f"{self.prefix}:status:{delivery_status}"

    def chunk_ledger_key(self, message_id: str) -> str:
        """Channel send-ledger key — the append-only list of chunks already accepted."""
        return f"{self.prefix}:progress:{message_id}"

    def outbound_index_key(self, channel: str, outbound_message_id: str) -> str:
        """Outbound-id → record reverse-index key, channel-qualified.

        An outbound provider id is unique only within its channel. Both halves must be non-blank.
        """
        _require_qualifier_segment("channel", channel)
        _require_key_segment("outbound_message_id", outbound_message_id)
        return f"{self.prefix}:outbound:{channel}:{outbound_message_id}"

    @property
    def outbound_index_key_prefix(self) -> str:
        """The outbound reverse-index key prefix (the segment every :meth:`outbound_index_key` starts with)."""
        return f"{self.prefix}:outbound:"

    def message_ref_key(self, channel: str, our_identity: str, client_address: str, medium_message_id: str) -> str:
        """Message-ref key: the ordered list of the record ``message_id``s that carry one medium message id.

        A medium id is unique only within one chat on some media, so the key names the channel, our
        identity and the participant address with it. Its variable part is a SINGLE opaque terminal
        segment, the sha256 of the deterministic JSON array of the four, because the address and the
        medium id are charset-unconstrained and so can never be raw non-terminal segments.
        """
        _require_key_segment("channel", channel)
        _require_key_segment("our_identity", our_identity)
        _require_key_segment("client_address", client_address)
        _require_key_segment("medium_message_id", medium_message_id)
        opaque = hashlib.sha256(
            json.dumps([channel, our_identity, client_address, medium_message_id], separators=(",", ":")).encode()
        ).hexdigest()
        return f"{self.message_ref_key_prefix}{opaque}"

    @property
    def message_ref_key_prefix(self) -> str:
        """The message-ref key prefix (the segment every :meth:`message_ref_key` starts with)."""
        return f"{self.prefix}:message_ref:"

    def ask_send_key(self, interaction_id: str) -> str:
        """Ask index key: an ask's ``interaction_id`` → the ``message_id`` of the send record that carried it."""
        _require_key_segment("interaction_id", interaction_id)
        return f"{self.ask_send_key_prefix}{interaction_id}"

    @property
    def ask_send_key_prefix(self) -> str:
        """The ask index key prefix (the segment every :meth:`ask_send_key` starts with)."""
        return f"{self.prefix}:ask_send:"

    def route_sending_key(self, route_name: str) -> str:
        """Per-route in-flight set: the ``message_id``s of the send records begun on the route and still ``sending``.

        The route name is checked exactly as :meth:`thread_index_key` checks it.
        """
        _require_qualifier_segment("route_name", route_name)
        return f"{self.prefix}:route_sending:{route_name}"

    def thread_index_key(self, route_name: str, thread_id: str) -> str:
        """Per-thread transcript index — the sorted set of one thread's ``message_id``s, scored by ``created_at``.

        So a transcript read never walks the record keyspace. The ``thread_id`` carries ``:`` of its own
        and so sits LAST.
        """
        _require_qualifier_segment("route_name", route_name)
        _require_key_segment("thread_id", thread_id)
        return f"{self.thread_index_key_prefix}{route_name}:{thread_id}"

    @property
    def thread_index_key_prefix(self) -> str:
        """The transcript-index key prefix the writer scripts build a thread's index key from in-script."""
        return f"{self.prefix}:thread:"

    def route_threads_key(self, route_name: str) -> str:
        """Per-route thread index — the sorted set of the route's ``thread_id``s, scored by last-active time.

        So a listing reads the newest first. The route name is checked exactly as :meth:`thread_index_key`
        checks it, so one name can never key one of the two thread indexes and be refused by the other.
        """
        _require_qualifier_segment("route_name", route_name)
        return f"{self.prefix}:route_threads:{route_name}"

    def route_key(self, route_name: str) -> str:
        """Routing-row key for a route name (a ``:``-free slug)."""
        return f"{self.prefix}:route:{route_name}"

    def mode_key(self, thread_id: str) -> str:
        """Per-thread mode-override key → ``agent``/``manual``.

        Absent = no override, so the route's ``initial_mode`` stands. The ``thread_id`` carries ``:`` of
        its own and so sits LAST.
        """
        _require_key_segment("thread_id", thread_id)
        return f"{self.prefix}:mode:{thread_id}"

    def thread_lease_key(self, thread_id: str) -> str:
        """Per-thread cross-worker turn-lease key → the worker token holding it.

        The mutex a turn holds for its whole run so two workers never fork one thread's checkpoint. The
        ``thread_id`` carries ``:`` of its own and so sits LAST.
        """
        _require_key_segment("thread_id", thread_id)
        return f"{self.prefix}:thread_lease:{thread_id}"

    @property
    def thread_lease_released_channel(self) -> str:
        """The pub/sub channel a released thread lease is announced on; each message is the thread id.

        A turn waiting for a busy thread listens here and retries the moment the holder releases.
        """
        return f"{self.prefix}:thread_lease_released"

    def overlap_cancel_key(self, thread_id: str) -> str:
        """Per-thread overlap-cancel marker key → the newest participant message's ``{message_id, created_at}``.

        Set on every accept for a ``running="cancel"`` route, TTL'd to the thread lease; the watcher a
        running turn arms reads it and cancels the turn when it names a message newer than the turn's
        batch. The ``thread_id`` carries ``:`` of its own and so sits LAST.
        """
        _require_key_segment("thread_id", thread_id)
        return f"{self.prefix}:overlap:cancel:{thread_id}"

    def owed_greeting_key(self, thread_id: str) -> str:
        """Per-thread owed first-contact greeting key → the rendered greeting text a turn still owes.

        Written when a first-contact greeting is minted for a turn and consumed by the first
        delivering MESSAGE turn on the thread (an event turn carries no greeting and leaves it owed),
        so a greeting whose minting turn was superseded or cancelled before it delivered rides the
        successor turn instead of being dropped. The ``thread_id`` carries ``:`` of its own and so
        sits LAST.
        """
        _require_key_segment("thread_id", thread_id)
        return f"{self.prefix}:overlap:greeting:{thread_id}"

    @property
    def route_key_prefix(self) -> str:
        """The route-row key prefix shared by every stored route (``<prefix>:route:``)."""
        return f"{self.prefix}:route:"

    @property
    def route_names_key(self) -> str:
        """The set index of every stored route name.

        Kept in lockstep with the per-route keys by the create/delete scripts.
        """
        return f"{self.prefix}:route_names"

    @property
    def route_version_key(self) -> str:
        """The route table's write token: set to a fresh value by every route write script.

        A reader's route snapshot is current while this key still holds the token it was loaded under.
        """
        return f"{self.prefix}:route_version"

    # -- Person-linking keyspaces --------------------------------------------

    def person_key(self, person_id: str) -> str:
        """Person-row key (JSON), keyed by the person's uuid4 ``person_id`` (terminal)."""
        _require_key_segment("person_id", person_id)
        return f"{self.prefix}:person:{person_id}"

    @property
    def person_key_prefix(self) -> str:
        """The person-row key prefix the merge/detach scripts build a row key from in-script.

        A survivor id chosen server-side is not known to the caller.
        """
        return f"{self.prefix}:person:"

    def person_erasing_key(self, person_id: str) -> str:
        """A person's erase mark (value ``"1"``, no TTL), set while its erase runs (terminal ``person_id``).

        A key of its own beside the row, deleted with the row in the erase's last step.
        """
        _require_key_segment("person_id", person_id)
        return f"{self.prefix}:person_erasing:{person_id}"

    @property
    def person_erasing_key_prefix(self) -> str:
        """The erase-mark key prefix the person scripts build a mark key from in-script."""
        return f"{self.prefix}:person_erasing:"

    def person_thread_routes_key(self, person_id: str) -> str:
        """A SET of the route names a person's thread is indexed under (terminal ``person_id``).

        Written in the same step as the person thread's index, so it names every route the thread
        holds records on.
        """
        _require_key_segment("person_id", person_id)
        return f"{self.prefix}:person_thread_routes:{person_id}"

    @property
    def person_thread_routes_key_prefix(self) -> str:
        """The person-thread-routes key prefix the person scripts build the key from in-script."""
        return f"{self.prefix}:person_thread_routes:"

    def person_absorbed_key(self, person_id: str) -> str:
        """A SET of every id a person absorbed by a link, the ids those absorbed in turn included.

        Terminal ``person_id``; no TTL.
        """
        _require_key_segment("person_id", person_id)
        return f"{self.prefix}:person_absorbed:{person_id}"

    def person_absorbed_into_key(self, person_id: str) -> str:
        """An absorbed person's pointer to the person that absorbed it by a link (terminal ``person_id``, no TTL)."""
        _require_key_segment("person_id", person_id)
        return f"{self.prefix}:person_absorbed_into:{person_id}"

    @property
    def person_absorbed_into_key_prefix(self) -> str:
        """The absorbed-into pointer prefix the person and record scripts build a pointer key from in-script."""
        return f"{self.prefix}:person_absorbed_into:"

    def writer_threads_key(self, writer: str) -> str:
        """A writer's threads: the append-only SET of the route-keyed threads it wrote in (no TTL).

        An erase of the writer visits each; an entry for a thread since cleared costs one visit that
        finds nothing. The ``writer`` is the ``person_id`` the target's person index named for the
        record's door at the write, or, while the index named nobody for it, the door itself: the
        self-delimiting JSON ``[target_kind, target_name, door_address_key]``, terminal.
        """
        _require_key_segment("writer", writer)
        return f"{self.prefix}:writer_threads:{writer}"

    @property
    def writer_threads_key_prefix(self) -> str:
        """The writer-threads prefix the record and person scripts build the key from in-script."""
        return f"{self.prefix}:writer_threads:"

    def writer_prefixes(self) -> str:
        """The JSON object of key prefixes the scripts that stamp or read a thread's writers build their keys from.

        Besides the person index and the writer threads: the record and transcript-index keys (an
        adopting person rewrites the writer its door's records carry, an erase reads them) and the
        route-keyed thread prefix (a thread's route, read in-script).
        """
        return json.dumps(
            {
                "index": self.person_index_key_prefix,
                "threads": self.writer_threads_key_prefix,
                "records": self.record_key_prefix,
                "thread_index": self.thread_index_key_prefix,
                "route_thread": BRIDGE_THREAD_PREFIX,
            },
            separators=(",", ":"),
        )

    def person_index_key(self, target_kind: str, target_name: str) -> str:
        """Per-target person index — a HASH ``door_address_key`` → ``person_id``.

        The ``target_name`` is charset-unconstrained and so sits TERMINAL; ``target_kind`` is a
        constrained qualifier checked ``:``-free.
        """
        _require_qualifier_segment("target_kind", target_kind)
        _require_key_segment("target_name", target_name)
        return f"{self.prefix}:person_index:{target_kind}:{target_name}"

    @property
    def person_index_key_prefix(self) -> str:
        """The person-index key prefix the merge/detach scripts build the index key from in-script.

        Once they read the target off the row (``<prefix>:person_index:``, then
        ``<target_kind>:<target_name>`` exactly as :meth:`person_index_key` renders it).
        """
        return f"{self.prefix}:person_index:"

    def pair_code_key(self, code_hash: str) -> str:
        """Single-use pair-code record key, keyed by ``sha256(code)`` (hex, terminal).

        The raw code is never a key nor a value anywhere — only its hash is.
        """
        _require_key_segment("code_hash", code_hash)
        return f"{self.prefix}:pair_code:{code_hash}"

    @property
    def pair_code_key_prefix(self) -> str:
        """The pair-code record key prefix the mint script builds the previously open code's key from in-script.

        It holds only that code's sha256, via the open-code pointer.
        """
        return f"{self.prefix}:pair_code:"

    def open_code_key(self, target_kind: str, target_name: str, door_address_key: str) -> str:
        """Open-pair-code pointer key for ONE minting conversation → the sha256 of the currently open code.

        Its variable part is a SINGLE opaque terminal segment — the sha256 of the deterministic JSON array
        ``[target_kind, target_name, door_address_key]`` — because ``target_name`` and the address folded
        into ``door_address_key`` are charset-unconstrained (may carry ``:``) and so can never be raw
        non-terminal segments; the hash removes all delimiter ambiguity while keeping an ordinary per-key
        TTL (a per-target HASH could not expire its fields).
        """
        _require_key_segment("target_kind", target_kind)
        _require_key_segment("target_name", target_name)
        _require_key_segment("door_address_key", door_address_key)
        opaque = hashlib.sha256(
            json.dumps([target_kind, target_name, door_address_key], separators=(",", ":")).encode()
        ).hexdigest()
        return f"{self.prefix}:open_code:{opaque}"

    # -- Per-target config keyspace ------------------------------------------

    def target_config_key(self, target_kind: str, target_name: str) -> str:
        """Per-target config row key, keyed by ``(target_kind, target_name)``.

        ``target_kind`` is a fixed vocabulary and sits before the free-form ``target_name``, so it is
        checked ``:``-free while the name sits LAST.
        """
        _require_qualifier_segment("target_kind", target_kind)
        _require_key_segment("target_name", target_name)
        return f"{self.prefix}:config:{target_kind}:{target_name}"

    @property
    def target_config_key_prefix(self) -> str:
        """The per-target config row key prefix shared by every stored config (``<prefix>:config:``)."""
        return f"{self.prefix}:config:"

    @property
    def target_config_names_key(self) -> str:
        """The set index of every stored config's ``{target_kind}:{target_name}`` member.

        Kept in lockstep with the per-config keys by the upsert/delete scripts. A member appended to
        :attr:`target_config_key_prefix` rebuilds the row key it names.
        """
        return f"{self.prefix}:config_names"

    @property
    def target_config_version_key(self) -> str:
        """The target-config map's write token: set to a fresh value by every config write script.

        A reader's config snapshot is current while this key still holds the token it was loaded under.
        """
        return f"{self.prefix}:target_config_version"

    # -- Redeem-throttle keyspace --------------------------------------------

    def _redeem_scope(self, target_kind: str, target_name: str, source_key: str) -> str:
        """The opaque terminal segment scoping a redeem throttle to ONE (target, source).

        The sha256 of the deterministic JSON array ``[target_kind, target_name, source_key]``.
        ``target_name`` and ``source_key`` (a door-qualified accountable-party encoding) are both
        charset-unconstrained, so the hash removes all delimiter ambiguity while keeping an ordinary
        per-key TTL.
        """
        _require_key_segment("target_kind", target_kind)
        _require_key_segment("target_name", target_name)
        _require_key_segment("source_key", source_key)
        return hashlib.sha256(
            json.dumps([target_kind, target_name, source_key], separators=(",", ":")).encode()
        ).hexdigest()

    def redeem_fail_key(self, target_kind: str, target_name: str, source_key: str) -> str:
        """Consecutive-invalid-redeem counter key for one (target, source).

        Cleared by a valid redeem and decays after the backoff cap of inactivity.
        """
        return f"{self.prefix}:redeem_fail:{self._redeem_scope(target_kind, target_name, source_key)}"

    def redeem_lock_key(self, target_kind: str, target_name: str, source_key: str) -> str:
        """Backoff-lock marker key for one (target, source).

        Present (with a TTL) exactly while that source's redeems are throttled after crossing the failure
        threshold.
        """
        return f"{self.prefix}:redeem_lock:{self._redeem_scope(target_kind, target_name, source_key)}"
