"""The per-target person registry.

The runtime state that folds several channel addresses into ONE identity for a single
``(target_kind, target_name)`` target.

Its Redis keyspaces, all built ONLY by the
:class:`~tai42_skeleton.conversations.keyspace.ConversationsKeyspace` builders:

- a PERSON-ROW key (``conversations:person:<person_id>``) → the :class:`Person` JSON.
- a per-target PERSON INDEX — a HASH ``conversations:person_index:<target_kind>:<target_name>``
  mapping a ``door_address_key`` → the ``person_id`` that owns that address.
- beside the row: the person's ERASE MARK (set while its erase runs; every person write refuses a
  marked person and every person read treats it as gone), its ABSORBED SET (every id a ``/link``
  merged into it, chains flattened), its thread's RECORDED ROUTES, and an absorbed id's
  ABSORBED-INTO pointer naming its survivor.
- per writer, its WRITER THREADS: the append-only visit list of the route-keyed threads it wrote
  in. Each record of a route-keyed thread keeps its writer, stamped in the record's own write (the
  person the target's index names for the record's door, else the door itself), so an erase can
  tell a thread only its person and the persons it absorbed wrote in from one another writer also
  wrote in, by reading the thread's records.

The ``door_address_key`` is a deterministic JSON array, the SINGLE canonical encoding of one
address, produced ONLY by :func:`_door_address_key`:

- a channel address → ``[channel, our_identity, address]``;
- an api address → ``["api", address]``.

Every part is charset-unconstrained (an address may carry ``:`` or quotes), so no hand-joined
delimiter form is ever used; :mod:`tai42_skeleton.conversations.pair_codes` imports this one
encoder rather than build its own, and no public store operation accepts a pre-built key — each
takes structured fields (``door, channel, our_identity, address`` or a :class:`PersonAddress`)
and derives the key internally, so no caller can ever encode divergently.

Every multi-key mutation is ONE Lua script (marker-commented, in
:mod:`tai42_skeleton.conversations.person_scripts`), so racing writers produce one outcome. The
scripts build the person-row and person-index keys from a prefix IN-SCRIPT (a survivor id or a
target read off a row is not known to the caller), which assumes a single-node Redis (not Cluster)
exactly as the record store and the trigger-link store do. cjson round-trips
a person row through pydantic on every read, so a Lua-re-encoded row stays valid.

Durability is the deployment's Redis persistence, exactly as for conversation records. Persons
are user-generated runtime state and are deliberately OUT of backup (operator config only), the
same contract that excludes conversation records — this module registers no backup section.
Every failure path raises; the sole documented soft outcome is a :meth:`get_person` miss.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from tai42_contract.conversations import (
    ConversationDoor,
    ConversationRoute,
    ConversationTargetKind,
    CrossTargetMergeError,
    NotLinkedError,
    Person,
    PersonAddress,
)
from tai42_contract.errors import UnavailableError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.person_scripts import (
    _DETACH_LUA,
    _ENSURE_LUA,
    _ERASE_LUA,
    _GET_BY_ID_LUA,
    _LOOKUP_LUA,
    _MARK_ERASING_LUA,
    _MERGE_LUA,
    _OUTSIDE_WRITER_LUA,
    _READ_FOR_ERASE_LUA,
    _SET_LOCALE_LUA,
)
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.thread_ids import RouteThreadId, parse_thread_id
from tai42_skeleton.operations.errors import ConflictError, NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

_NO_BACKEND = "conversation persons require the redis conversations backend"


@dataclass(frozen=True)
class PairingTarget:
    """The ``(target_kind, target_name)`` a person is scoped to — persons never cross it."""

    target_kind: ConversationTargetKind
    target_name: str


def _door_address_key(*, door: ConversationDoor, channel: str | None, our_identity: str | None, address: str) -> str:
    """The single canonical encoding of one address for the person index and the open-code pointer.

    ``[channel, our_identity, address]`` for a channel address, ``["api", address]`` for an api
    address. Deterministic JSON (no delimiter joining), so charset-unconstrained parts never collide.
    """
    if door == "channel":
        if channel is None or our_identity is None:
            raise ValueError("a channel address must carry channel and our_identity")
        return json.dumps([channel, our_identity, address], separators=(",", ":"))
    if channel is not None or our_identity is not None:
        raise ValueError("an api address carries no channel/our_identity")
    return json.dumps(["api", address], separators=(",", ":"))


def thread_door(route: ConversationRoute, thread_id: str) -> str | None:
    """The door a write into ``thread_id`` under ``route`` is stamped for, or ``None`` for a person thread.

    The JSON ``[target_kind, target_name, door_address_key]`` of the route's target and the address
    the route-keyed thread id carries, on the route's door, channel and identity: the address the
    doors pass to this store. The write's writer is the person the target's index names for that
    door address key, or the door itself while the index names nobody.
    """
    parsed = parse_thread_id(thread_id)
    if not isinstance(parsed, RouteThreadId):
        return None
    door_address_key = _door_address_key(
        door=route.door, channel=route.channel, our_identity=route.our_identity, address=parsed.client_address
    )
    return json.dumps([route.target_kind, route.target_name, door_address_key], separators=(",", ":"))


def _address_key_of(address: PersonAddress) -> str:
    return _door_address_key(
        door=address.door, channel=address.channel, our_identity=address.our_identity, address=address.address
    )


def _as_str(value: Any) -> str:
    """Normalize Redis's ``bytes``-or-``str`` return to ``str`` (the row JSON is utf-8)."""
    return value.decode() if isinstance(value, bytes) else value


class PersonErasingError(UnavailableError):
    """A person write refused because the person's erase is running (503): nothing was changed."""

    def __init__(self, person_id: str, what: str) -> None:
        """Name the person being erased and ``what`` was refused."""
        super().__init__(
            f"conversation person {person_id!r} is being erased: {what} was refused and nothing was changed; "
            "retry once the erase has finished"
        )
        self.person_id = person_id


@dataclass(frozen=True)
class PersonEraseRead:
    """A person, what it absorbed, where it points and the route threads they wrote in, read in one step.

    ``person`` is ``None`` when the row is gone. ``absorbed_ids`` are the sorted ids the person
    absorbed by a link; ``absorbed_into`` is the id of the person that absorbed it, when one did.
    ``thread_routes`` maps the person and each absorbed id to the sorted routes its person thread is
    cleared across: the person's addresses' routes and its recorded routes, and for an absorbed id
    its own recorded routes with the person's addresses' routes (``[]`` for the person with no row).
    ``route_threads`` are the sorted route-keyed threads the person or an absorbed id is a writer of.
    """

    person: Person | None
    absorbed_ids: list[str]
    absorbed_into: str | None
    thread_routes: dict[str, list[str]]
    route_threads: list[str]


class ConversationPersonStore:
    """The Redis-backed per-target person registry.

    Construction refuses with a loud 501 without the redis conversations backend — a person folded
    across channels must not live in per-worker state that vanishes with the process.
    """

    def __init__(self, settings: ConversationsSettings) -> None:
        """Store ``settings``, refusing construction (501) without the redis conversations backend."""
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def get_person(
        self,
        target: PairingTarget,
        *,
        door: ConversationDoor,
        channel: str | None,
        our_identity: str | None,
        address: str,
    ) -> Person | None:
        """The person owning ``address`` on ``target``, or ``None`` when the address has never been seen there.

        The index field and its row are read in one atomic step; an index field naming a missing
        row is corruption and raises. An owner whose erase is running also answers ``None``: its
        addresses are attributed as unlinked.
        """
        dak = _door_address_key(door=door, channel=channel, our_identity=our_identity, address=address)
        index_key = self.settings.keyspace.person_index_key(target.target_kind, target.target_name)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _LOOKUP_LUA,
                1,
                index_key,
                dak,
                self.settings.keyspace.person_key_prefix,
                self.settings.keyspace.person_erasing_key_prefix,
            )
        status = _as_str(result[0])
        if status in ("miss", "erasing"):
            return None
        if status == "hit":
            return Person.model_validate_json(_as_str(result[1]))
        raise RuntimeError(f"conversations: person index names a missing row: {_as_str(result[1])!r}")

    async def get_by_id(self, person_id: str, *, include_erasing: bool = False) -> Person | None:
        """The person row named by ``person_id``, or ``None`` when no such row exists.

        The aggregated person-thread read door resolves the ``bridge:@person:{person_id}`` key
        this way — against the store, never by parsing addresses out of the thread id. A person
        whose erase is running answers ``None`` too, unless ``include_erasing`` asks for its row.
        The row and the mark are read in one step.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _GET_BY_ID_LUA,
                2,
                self.settings.keyspace.person_key(person_id),
                self.settings.keyspace.person_erasing_key(person_id),
            )
        raw = _as_str(result[0])
        if not raw or (int(result[1]) == 1 and not include_erasing):
            return None
        return Person.model_validate_json(raw)

    async def mark_erasing(self, person_id: str) -> bool:
        """Set ``person_id``'s erase mark while its row stands; ``False`` (nothing set) when the row is gone.

        From the mark on, every person write refuses the person with :class:`PersonErasingError`,
        its addresses are attributed as unlinked and its thread takes no new record.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _MARK_ERASING_LUA,
                2,
                self.settings.keyspace.person_key(person_id),
                self.settings.keyspace.person_erasing_key(person_id),
            )
        return int(result) == 1

    async def clear_erasing(self, person_id: str) -> None:
        """Remove ``person_id``'s erase mark (an erase refused before its teardown gives the person back)."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.delete(self.settings.keyspace.person_erasing_key(person_id)))

    async def is_erasing(self, person_id: str) -> bool:
        """Whether ``person_id``'s erase mark stands."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(await awaited(r.exists(self.settings.keyspace.person_erasing_key(person_id)))) == 1

    async def thread_routes(self, person_id: str) -> set[str]:
        """The route names ``person_id``'s aggregated thread is indexed under, as its writes recorded them."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            members = await awaited(r.smembers(self.settings.keyspace.person_thread_routes_key(person_id)))
        return {_as_str(member) for member in members}

    async def read_for_erase(self, person_id: str) -> PersonEraseRead:
        """Read ``person_id``'s row, absorbed ids, recorded routes, pointer and writer threads in one step.

        Raises ``RuntimeError`` when the pointer names a survivor with no row, and ``ValueError`` when
        the row is unreadable.
        """
        settings = self.settings
        async with client_ctx(RedisClient, settings.redis) as r:
            result = await eval_script(
                r,
                _READ_FOR_ERASE_LUA,
                4,
                settings.keyspace.person_key(person_id),
                settings.keyspace.person_absorbed_key(person_id),
                settings.keyspace.person_thread_routes_key(person_id),
                settings.keyspace.person_absorbed_into_key(person_id),
                settings.keyspace.person_thread_routes_key_prefix,
                settings.keyspace.person_key_prefix,
                settings.keyspace.writer_threads_key_prefix,
                person_id,
            )
        raw_row, pointer, survivor_stands, recorded, absorbed_rows, route_threads = result
        row = _as_str(raw_row)
        survivor = _as_str(pointer) or None
        if survivor is not None and int(survivor_stands) != 1:
            raise RuntimeError(
                f"conversations: person {person_id!r} points at an absorbing person with no row: {survivor!r}"
            )
        person = Person.model_validate_json(row) if row else None
        address_routes: set[str] = set()
        thread_routes: dict[str, list[str]] = {person_id: []}
        if person is not None:
            address_routes = {route for address in person.addresses for route in address.routes}
            thread_routes[person_id] = sorted(address_routes | {_as_str(route) for route in recorded})
        for absorbed_id_raw, absorbed_routes in absorbed_rows:
            absorbed_id = _as_str(absorbed_id_raw)
            thread_routes[absorbed_id] = sorted(address_routes | {_as_str(route) for route in absorbed_routes})
        return PersonEraseRead(
            person=person,
            absorbed_ids=sorted(_as_str(row_id) for row_id, _ in absorbed_rows),
            absorbed_into=survivor,
            thread_routes=thread_routes,
            route_threads=sorted(_as_str(thread) for thread in route_threads),
        )

    async def outside_writer(self, thread_id: str, erased: set[str]) -> str | None:
        """The first writer outside ``erased`` that keeps route-keyed ``thread_id`` standing, read in one step.

        Reads the ``writer`` of every record the thread holds, judged against the person rows, the
        erase marks and the absorbed-into pointers as they stand now: an id of ``erased`` is going;
        a door nobody held when it wrote keeps the thread; a person whose row stands keeps it unless
        its erase mark stands; a person a link absorbed is judged by its survivor (an id of
        ``erased`` or a marked survivor is going); a person with neither row nor pointer was erased.
        Answers ``None`` when nothing keeps the thread standing. Raises ``TypeError`` for a thread
        that is not route-keyed, and a Redis error naming the record or the person when a record
        carries no writer or a pointer names a survivor with no row.
        """
        if not isinstance(parse_thread_id(thread_id), RouteThreadId):
            raise TypeError(f"conversations: {thread_id!r} is not a route-keyed thread")
        settings = self.settings
        async with client_ctx(RedisClient, settings.redis) as r:
            outside = await eval_script(
                r,
                _OUTSIDE_WRITER_LUA,
                0,
                thread_id,
                settings.keyspace.writer_prefixes(),
                settings.keyspace.person_key_prefix,
                settings.keyspace.person_erasing_key_prefix,
                settings.keyspace.person_absorbed_into_key_prefix,
                *sorted(erased),
            )
        return _as_str(outside) or None

    async def ensure_provisional(
        self, target: PairingTarget, address_row: PersonAddress, *, locale: str | None = None
    ) -> tuple[Person, bool]:
        """Get-or-create the single-address person for ``address_row`` on ``target``.

        Atomic against a concurrent first contact. On an EXISTING person, unions ``address_row``'s
        routes into its matching address and leaves its stored ``locale`` untouched (a later
        turn never clobbers an operator override). Returns ``(person, created)`` — ``created``
        is True ONLY when this call wrote the row, the first-contact signal a greeting keys
        off. ``locale`` seeds a NEWLY created person's stored locale (the channel's
        first-contact hint); it is ignored when the person already exists. An existing person whose
        erase is running raises :class:`PersonErasingError` with nothing written. Otherwise the person
        adopts what the address's door wrote while nobody held it: every record the door wrote in a
        route-keyed thread names the person as its writer instead, and the door's visit list becomes
        the person's.
        """
        new_id = str(uuid4())
        person = Person(
            person_id=new_id,
            target_kind=target.target_kind,
            target_name=target.target_name,
            created_at=datetime.now(UTC),
            addresses=[address_row],
            locale=locale,
        )
        dak = _address_key_of(address_row)
        index_key = self.settings.keyspace.person_index_key(target.target_kind, target.target_name)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _ENSURE_LUA,
                1,
                index_key,
                dak,
                self.settings.keyspace.person_key_prefix,
                new_id,
                person.model_dump_json(),
                address_row.door,
                address_row.channel or "",
                address_row.our_identity or "",
                address_row.address,
                json.dumps(address_row.routes, separators=(",", ":")),
                self.settings.keyspace.person_erasing_key_prefix,
                json.dumps([target.target_kind, target.target_name, dak], separators=(",", ":")),
                self.settings.keyspace.writer_prefixes(),
            )
        status = _as_str(result[0])
        if status == "created":
            return Person.model_validate_json(_as_str(result[1])), True
        if status == "existing":
            return Person.model_validate_json(_as_str(result[1])), False
        if status == "erasing":
            raise PersonErasingError(_as_str(result[1]), f"a new message from {address_row.address!r}")
        raise RuntimeError(f"conversations: person index names a missing row: {_as_str(result[1])!r}")

    async def erase(self, person: Person, absorbed_ids: list[str], visited: list[str]) -> tuple[bool, int]:
        """Erase ``person`` from the registry ENTIRELY.

        Removes its person row, its erase mark, its absorbed SET, its recorded routes, the recorded
        routes and absorbed-into pointer of each of ``absorbed_ids`` (the persons it absorbed), every
        ``door_address_key → person_id`` field of its per-target index (removed by value, so exactly
        this person's addresses go, whatever the row currently lists), and the visit lists
        (``writer_threads``) of the person and of each absorbed id. ``visited`` lists the route-keyed
        threads the erase visited: a thread one of those visit lists names outside it (a write that
        landed while the erase ran) raises :class:`ConflictError` (409) with nothing removed, so the
        mark stands and a retry visits it. Each visited thread that no writer outside them keeps
        standing and that still holds a record written by the person or an absorbed id (a write
        that landed after the erase cleared the thread) is added to the visit list of that record's
        door, so the next person to hold the door adopts it and that person's erase reaches it. A
        thread a writer outside keeps standing is reached by that writer's erase. A record keeps the
        writer it was written with. One atomic unit, so a concurrent lookup never sees the row gone
        with an index still naming it. Idempotent: a re-run on an already-erased person removes
        nothing. Returns ``(row_removed, index_fields_removed)``.
        """
        settings = self.settings
        async with client_ctx(RedisClient, settings.redis) as r:
            result = await eval_script(
                r,
                _ERASE_LUA,
                5,
                settings.keyspace.person_key(person.person_id),
                settings.keyspace.person_index_key(person.target_kind, person.target_name),
                settings.keyspace.person_erasing_key(person.person_id),
                settings.keyspace.person_absorbed_key(person.person_id),
                settings.keyspace.person_thread_routes_key(person.person_id),
                person.person_id,
                settings.keyspace.person_thread_routes_key_prefix,
                settings.keyspace.person_absorbed_into_key_prefix,
                settings.keyspace.writer_prefixes(),
                json.dumps(sorted(visited), separators=(",", ":")),
                settings.keyspace.person_key_prefix,
                settings.keyspace.person_erasing_key_prefix,
                *absorbed_ids,
            )
        status = _as_str(result[0])
        if status == "unvisited":
            raise ConflictError(
                f"a write by {_as_str(result[2])!r} in the thread {_as_str(result[1])!r} landed while the erase of "
                f"{person.person_id!r} ran; retry the erase, which clears that thread too"
            )
        if status != "ok":
            raise RuntimeError(f"conversations: erase returned an unexpected status {status!r}")
        return bool(int(result[1])), int(result[2])

    async def set_locale(self, person_id: str, locale: str | None) -> Person | None:
        """Set ``person_id``'s stored locale to ``locale`` (canonical BCP 47) or clear it with ``None``.

        Updated in place on the person row; returns the updated person, or ``None`` when no such
        person exists. The one WRITE door an operator drives to override the channel-seeded locale;
        a later turn's ``ensure_provisional`` never clobbers it. A person whose erase is running
        raises :class:`PersonErasingError` with nothing written.
        """
        row_key = self.settings.keyspace.person_key(person_id)
        mode = "clear" if locale is None else "set"
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r, _SET_LOCALE_LUA, 2, row_key, self.settings.keyspace.person_erasing_key(person_id), mode, locale or ""
            )
        status = _as_str(result[0])
        if status == "missing":
            return None
        if status == "erasing":
            raise PersonErasingError(person_id, "the locale change")
        if status == "ok":
            return Person.model_validate_json(_as_str(result[1]))
        raise RuntimeError(f"conversations: set_locale returned an unexpected status {status!r}")

    async def merge(self, person_id_a: str, person_id_b: str) -> Person:
        """Merge two persons of the SAME target into one and return the survivor.

        The store picks the survivor (earlier ``created_at``; ties by lexically smaller ``person_id``),
        unions the absorbed addresses, repoints the absorbed index entries, and deletes the
        absorbed row — one atomic unit, so callers never decide the survivor. The same step adds the
        absorbed person and every id it had absorbed to the survivor's absorbed SET and points each of
        them at the survivor; the threads' writer stamps are left as they are. ``merge(P, P)`` (both
        ids the same live person) is a no-op returning P. A cross-target merge raises
        :class:`CrossTargetMergeError`; a person whose erase is running raises
        :class:`PersonErasingError`, with nothing written.
        """
        settings = self.settings
        async with client_ctx(RedisClient, settings.redis) as r:
            result = await eval_script(
                r,
                _MERGE_LUA,
                6,
                settings.keyspace.person_key(person_id_a),
                settings.keyspace.person_key(person_id_b),
                settings.keyspace.person_erasing_key(person_id_a),
                settings.keyspace.person_erasing_key(person_id_b),
                settings.keyspace.person_absorbed_key(person_id_a),
                settings.keyspace.person_absorbed_key(person_id_b),
                settings.keyspace.person_index_key_prefix,
                settings.keyspace.person_absorbed_into_key_prefix,
            )
        status = _as_str(result[0])
        if status == "ok":
            return Person.model_validate_json(_as_str(result[1]))
        if status == "erasing":
            raise PersonErasingError(_as_str(result[1]), "the merge")
        if status == "cross_target":
            raise CrossTargetMergeError(f"cannot merge persons across targets ({person_id_a!r}, {person_id_b!r})")
        if status == "duplicate_address":
            raise RuntimeError(
                "conversations: merge found an address owned by both persons — the per-target "
                f"index's single-ownership is corrupt: {_as_str(result[1])!r}"
            )
        raise RuntimeError(f"conversations: merge names a missing person row: {_as_str(result[1])!r}")

    async def detach(
        self,
        person_id: str,
        *,
        door: ConversationDoor,
        channel: str | None,
        our_identity: str | None,
        address: str,
    ) -> Person:
        """Detach one address from a multi-address person and return it as its own fresh provisional person.

        The rest of the original stays linked. Refuses with :class:`NotLinkedError` when the address
        is the person's only one — it is already provisional, so there is nothing to unlink. The
        detached address's fresh row carries its accumulated routes, and being a fresh row means its
        next inbound is not a first-contact creation, so no greeting re-fires. A person whose erase
        is running raises :class:`PersonErasingError` with nothing written.
        """
        new_id = str(uuid4())
        dak = _door_address_key(door=door, channel=channel, our_identity=our_identity, address=address)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _DETACH_LUA,
                3,
                self.settings.keyspace.person_key(person_id),
                self.settings.keyspace.person_key(new_id),
                self.settings.keyspace.person_erasing_key(person_id),
                self.settings.keyspace.person_index_key_prefix,
                dak,
                new_id,
                datetime.now(UTC).isoformat(),
                door,
                channel or "",
                our_identity or "",
                address,
            )
        status = _as_str(result[0])
        if status == "ok":
            return Person.model_validate_json(_as_str(result[1]))
        if status == "erasing":
            raise PersonErasingError(person_id, "the detach")
        if status == "not_linked":
            raise NotLinkedError(f"address is not part of a multi-address person: {person_id!r}")
        if status == "not_member":
            raise RuntimeError(f"conversations: detach asked to unlink an address not owned by person {person_id!r}")
        raise RuntimeError(f"conversations: detach names a missing person row: {_as_str(result[1])!r}")
