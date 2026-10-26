"""Thread and person forget doors.

The absolute, idempotent teardown of a thread's checkpoint, answer records and indexes
(route-keyed or linked-person aggregate), and the whole-person erase.
"""

from __future__ import annotations

import sys
from typing import Any

from tai42_contract.conversations import Person
from tai42_contract.errors import UnavailableError
from tai42_contract.states import PERSON_KIND

from tai42_skeleton.app import instance
from tai42_skeleton.conversations.person_thread import person_routes
from tai42_skeleton.conversations.persons import PersonEraseRead
from tai42_skeleton.conversations.thread_ids import (
    RouteThreadId,
    ThreadIdError,
    parse_thread_id,
    person_thread_id,
    route_thread_id,
)
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import ConflictError, NotSupportedError
from tai42_skeleton.operations.response_models.conversations import PersonDeleteResult, ThreadDeleteResult
from tai42_skeleton.states import states_store_configured

from .backend import _require_backend, _thread_not_found
from .models import ThreadDeleteQuery
from .routes import _validate_route_name

# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``.
_pkg = sys.modules["tai42_skeleton.operations.conversations"]


async def _thread_delete_routes(thread_id: str, route_name: str) -> list[str]:
    """The route indexes ``thread_id`` is forgotten across.

    A route-keyed thread lives under
    its one ``route_name`` and its id MUST carry that route's ``bridge:{route_name}:`` prefix
    — a route slug carries no ``:``, so the prefix is unambiguous; the check is the sole guard
    stopping a delete on one route from reaching another route's thread memory by id, and a
    mismatch is a loud 400. A LINKED person's aggregated thread (``bridge:@person:{id}``) lives
    across EVERY route the person wrote under, and the supplied ``route_name`` must be one of
    them (else a loud 404), so the delete reaches only a thread the person actually holds and
    every route index carrying it is reclaimed (else a member strands under one). A person
    thread's routes are the union of the person's addresses' routes and the routes its thread's
    writes recorded, which also name a route an ``/unlink`` took off the person and a route only a
    send used. Caller authority is the door's grantable write action — the same grant that
    creates a route — never derived here.
    """
    try:
        parsed = parse_thread_id(thread_id)
    except ThreadIdError as exc:
        raise _not_a_thread_of(thread_id, route_name) from exc
    if isinstance(parsed, RouteThreadId):
        if parsed.route_name != route_name:
            raise _not_a_thread_of(thread_id, route_name)
        return [route_name]
    persons = _require_backend().persons
    person = await persons.get_by_id(parsed.person_id)
    if person is None:
        raise _thread_not_found(thread_id)
    routes = person_routes(person) | await persons.thread_routes(parsed.person_id)
    if route_name not in routes:
        raise _thread_not_found(thread_id)
    return sorted(routes)


async def _clear_subjects(targets: list[tuple[str, str]], subjects: list[tuple[str, str]]) -> int:
    """Erase ``subjects`` across ``targets`` through the states service; the records erased.

    The forget's state half, reached ONLY through the states service seam — conversations never
    touches ``state_records`` directly. Every target's pending state saves are DRAINED first:
    a write-before-reply save still unapplied in the outbox has not landed a row yet, so
    ``clear_subject`` (which discovers states from landed rows and aliases) would miss it and the
    conversation would later ``remember`` what the reset forgot. Draining lands them first, so the
    erase then sees and forgets them. A save that cannot be drained (held behind or itself a failed
    save) is surfaced LOUDLY as a retriable error rather than silently left to resurface — the
    thread/person is already forgotten, its stuck state remains, and a retry (idempotent) finishes
    it once the save settles.
    """
    unsettled: list[str] = []
    for target_kind, target_name in targets:
        unsettled += await instance.app.states.drain_target_saves(target_kind, target_name)
    if unsettled:
        raise UnavailableError(
            "conversation reset cannot forget declared state while pending saves are unsettled; "
            "retry once they settle: " + "; ".join(unsettled)
        )
    erased = 0
    for target_kind, target_name in targets:
        for subject_kind, subject_key in subjects:
            erased += await instance.app.states.clear_subject(target_kind, target_name, subject_kind, subject_key)
    return erased


async def _route_state_targets(route_names: list[str]) -> list[tuple[str, str]]:
    """The distinct ``(target_kind, target_name)`` scopes the routes resolve to.

    A turn keys a thread's state under its route's target, so the forget clears state under each
    distinct target the thread's routes resolve to. A route no longer present resolves to no
    target — the forget stays absolute, as the index teardown is on a route already gone.
    """
    manager = _require_backend()
    targets: dict[tuple[str, str], None] = {}
    for name in route_names:
        route = await manager.get_route(name)
        if route is not None:
            targets[(route.target_kind, route.target_name)] = None
    return list(targets)


async def _erase_thread_state(
    route_names: list[str], thread_id: str, also_targets: tuple[tuple[str, str], ...] = ()
) -> int:
    """Forget the thread's own (``thread``-subject) declared-state memory across its routes' targets.

    ``also_targets`` adds scopes the thread's state may sit under besides its routes' current
    targets (the erase adds the erased person's target, which a route rewritten since no longer
    resolves to). Runs LAST under the thread FIFO. A no-op (0) when the states feature is
    unconfigured — there is then no declared state to forget, and no route lookup is made —
    mirroring the parked-ask cascade's unconfigured no-op.
    """
    if not states_store_configured():
        return 0
    targets = dict.fromkeys([*await _route_state_targets(route_names), *also_targets])
    return await _clear_subjects(list(targets), [("thread", thread_id)])


async def _erase_person_state(target_kind: str, target_name: str, person_id: str, absorbed_ids: list[str]) -> int:
    """Forget a person's declared-state memory, and that of every person it absorbed, on the person's one target.

    For the person and each absorbed id: its own (``person``-subject) cross-thread memory AND its
    aggregated thread's (``thread``-subject) memory. First it waits out every person-subject write
    admitted before the erase mark (``settle_person_writes``, for the person and then each absorbed id
    in order), so each such write has landed or is a pending save when the drain and the clear run,
    and every later one is refused by the mark. A no-op (0) when the states feature is unconfigured,
    like :func:`_erase_thread_state`.
    """
    if not states_store_configured():
        return 0
    subjects: list[tuple[str, str]] = []
    for subject_id in [person_id, *absorbed_ids]:
        await instance.app.states.settle_person_writes(target_kind, target_name, subject_id)
        subjects += [("thread", person_thread_id(subject_id)), (PERSON_KIND, subject_id)]
    return await _clear_subjects([(target_kind, target_name)], subjects)


def _not_a_thread_of(thread_id: str, route_name: str) -> BadRequestError:
    """The 400 for a thread id that is not a thread of ``route_name``."""
    prefix = route_thread_id(route_name, "")
    return BadRequestError(
        f"thread_id {thread_id!r} is not a thread of route {route_name!r}: it must start with {prefix!r}"
    )


async def _delete_thread_checkpoint(thread_id: str) -> None:
    """Delete ``thread_id``'s agent checkpoint on the configured provider.

    The run's actual memory, reached the way the retention sweep reaches it. Every provider's saver
    must expose ``adelete_thread``; one that does not is a loud 501, never a silent skip that would
    leave the forgotten thread's memory behind.
    """
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
    from tai42_kit.llm.settings import llm_provider_settings

    settings = llm_provider_settings()
    saver = await checkpoint_registry().get_checkpointer(
        provider=settings.checkpoint, conn_string=settings.checkpoint_conn_string
    )
    adelete_thread = getattr(saver, "adelete_thread", None)
    if adelete_thread is None:
        raise NotSupportedError(
            f"checkpoint provider {settings.checkpoint!r} exposes no adelete_thread, so a conversation "
            "thread's memory cannot be forgotten on it"
        )
    await adelete_thread(thread_id)


async def _tear_down_thread(
    route_names: list[str], thread_id: str, *, reason: str, also_targets: tuple[tuple[str, str], ...] = ()
) -> tuple[int, int]:
    """Forget one thread under its FIFO; ``(records removed, state records erased)``.

    Cancels every async ``ask`` parked on the thread BEFORE the indexes go, so the forget does not
    orphan a park (its expiry reaper would later fire a continuation into the forgotten thread — a
    delivery retry storm — and its channel correlation would mute the participant's number until the
    deadline); then deletes its checkpoint, drops its records and indexes on each of ``route_names``,
    and last forgets its ``thread``-subject declared state (see :func:`_erase_thread_state`). Every
    step is idempotent, so a retry re-runs it whole.
    """
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    store = _require_backend().records
    await cancel_parks_for_thread(thread_id, reason=reason)
    await _delete_thread_checkpoint(thread_id)
    removed = 0
    for name in route_names:
        removed += await store.drop_thread(name, thread_id)
    states_erased = await _erase_thread_state(route_names, thread_id, also_targets)
    return removed, states_erased


@operation(
    summary="Delete a conversation thread",
    tags=["conversations"],
    errors=[BadRequestError, ConflictError, NotFoundError, NotSupportedError, UnavailableError],
    query_model=ThreadDeleteQuery,
    response_model=ThreadDeleteResult,
)
async def delete_conversation_thread(route_name: str, thread_id: str) -> dict[str, Any]:
    """Forget ONE conversation thread: its agent checkpoint, its answer records and its thread indexes.

    A later message on the same address starts a memory the deleted turns never touched. A LINKED
    person's aggregated ``bridge:@person:{id}`` thread is forgotten across every route index it
    spans.

    Forgetting is ABSOLUTE: a valid id on its own route always succeeds, even when nothing is
    stored. An aged-out thread whose answer records already expired under the retention TTL,
    or one never seen, answers ``removed=0`` and never a 404 — and its checkpoint is deleted
    regardless, because that memory defaults to keep-forever and is otherwise left behind once
    the records lapse. A route-keyed id MUST carry the route's ``bridge:{route_name}:`` prefix;
    an id belonging to another route is a loud 400, the sole guard stopping a delete on one
    route from wiping another route's memory. A person ``bridge:@person:{id}`` thread whose
    person is unknown, or whose ``route_name`` is not one of the person's routes, is a loud
    404. A turn IN FLIGHT on the thread (an ``accepted`` intake still holding a live lease) is
    a 409: its completion re-writes checkpoint state and re-stamps the indexes behind the
    delete, half-forgetting the memory — retry once it drains. The guard is re-checked under
    the FIFO, so a turn admitted while the lock is being taken is refused before any teardown,
    never left to re-create memory behind the delete. A ``route_name`` that is not a valid
    slug, or a blank ``thread_id``, is a 400.

    Caller authority is the door's grantable write action — the SAME write grant that creates
    a route deletes routes and forgets threads — never a per-thread owner check.

    The teardown runs in an order an interruption cannot strand: the checkpoint FIRST — its
    memory is reachable only through the indexes once this call returns, so it must go while
    the indexes still name the thread to bring a re-run back — then the records and the indexes
    together, the index being the durable marker of an owed reclamation and torn down LAST.
    Idempotent under re-run, so a partial completion is FINISHED by a retry.

    The whole teardown runs under the thread's per-thread FIFO (the lock an operator send and
    an in-flight turn take), so an operator send already in flight on the thread within this
    worker drains BEFORE the delete rather than half-behind it; a full queue is the loud,
    retriable 503 that FIFO raises before anything is torn down. As a live-caller sync door the
    acquisition is bounded by ``sync_door_wait_seconds``: a wait past it — behind a turn possibly
    HITL-paused on another worker — is the loud, retriable 503 ``ThreadBusyError`` rather than a
    block past the proxy timeout.

    Returns ``{"removed", "route_name", "thread_id", "states_erased"}``, where ``removed`` counts
    the answer records this call deleted (0 when a prior run already cleared them, when their rows
    had expired under the retention TTL, or when the id was never stored) and ``states_erased``
    counts the thread-subject declared-state records forgotten across the routes the thread spans
    (0 when the states feature is off or the thread held no declared state).

    A send still going out on the thread when this call runs is forgotten when it ends, not by
    this call: for at most one settle deadline its ``sending`` row, in no thread index, still holds
    the participant's address, and ``removed`` does not count it.
    """
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    store = _require_backend().records
    from tai42_skeleton.conversations.caps import get_turn_caps

    route_names = await _thread_delete_routes(thread_id, route_name)
    in_flight_409 = f"conversation thread {thread_id!r} has a turn in flight; retry once it drains"
    if await store.thread_has_live_intake(thread_id):
        raise ConflictError(in_flight_409)
    caps = get_turn_caps()
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        # Re-check UNDER the FIFO: an intake admitted between the outer check and taking the
        # lock is durably visible here, so its turn — queued behind this lock — is refused
        # before any teardown, never left to run after the delete and re-create the checkpoint.
        if await store.thread_has_live_intake(thread_id):
            raise ConflictError(in_flight_409)
        # The thread's own declared-state memory goes last (``thread``-subject only; state a binding
        # scopes to the ``person`` is cross-thread memory that an "Erase person" clears).
        removed, states_erased = await _tear_down_thread(route_names, thread_id, reason="thread_deleted")
    return {"removed": removed, "route_name": route_name, "thread_id": thread_id, "states_erased": states_erased}


def _absorbed_into_conflict(person_id: str, survivor: str) -> ConflictError:
    """The 409 for an id a link absorbed into ``survivor``: its erase is the survivor's."""
    return ConflictError(
        f"conversation person {person_id!r} was absorbed into person {survivor!r} by a link and is not erased "
        f"on its own: erase {survivor!r}, which clears {survivor!r} and every person it absorbed, "
        f"{person_id!r} included",
        extra={"absorbed_into_person_id": survivor},
    )


def _thread_in_flight(person_id: str, thread_id: str) -> ConflictError:
    """The 409 for a turn in flight in a thread the erase of ``person_id`` clears besides its own."""
    return ConflictError(
        f"conversation person {person_id!r} has a turn in flight in thread {thread_id!r}; retry once it drains"
    )


def _own_in_flight(person_id: str) -> str:
    return f"conversation person {person_id!r} has a turn in flight; retry once it drains"


def _route_of(thread_id: str) -> str:
    """The route of a route-keyed thread a writer's threads name; ``RuntimeError`` for any other id."""
    parsed = parse_thread_id(thread_id)
    if isinstance(parsed, RouteThreadId):
        return parsed.route_name
    raise RuntimeError(f"conversations: a writer's threads name {thread_id!r}, which is not route-keyed")


async def _check_erase_intake(store: Any, person_id: str, read: PersonEraseRead) -> None:
    """Raise the 409 of the first thread the erase clears that has a turn in flight, in order.

    The person's own thread, then each absorbed person's thread, then each route thread no writer
    outside the person and the ids it absorbed keeps standing (:meth:`outside_writer`).
    """
    if await store.thread_has_live_intake(person_thread_id(person_id)):
        raise ConflictError(_own_in_flight(person_id))
    for absorbed_id in read.absorbed_ids:
        if await store.thread_has_live_intake(person_thread_id(absorbed_id)):
            raise _thread_in_flight(person_id, person_thread_id(absorbed_id))
    erased = {person_id, *read.absorbed_ids}
    persons = _require_backend().persons
    for thread in read.route_threads:
        if await persons.outside_writer(thread, erased) is None and await store.thread_has_live_intake(thread):
            raise _thread_in_flight(person_id, thread)


async def _clear_absorbed_thread(person_id: str, absorbed_id: str, routes: list[str], caps: Any) -> int:
    """Clear an absorbed person's aggregated thread under its hold; the records removed."""
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    store = _require_backend().records
    thread = person_thread_id(absorbed_id)
    if await store.thread_has_live_intake(thread):
        raise _thread_in_flight(person_id, thread)
    removed = 0
    async with caps.hold_thread(thread, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        if await store.thread_has_live_intake(thread):
            raise _thread_in_flight(person_id, thread)
        await cancel_parks_for_thread(thread, also_subjects=[(PERSON_KIND, absorbed_id)], reason="person_erased")
        await _delete_thread_checkpoint(thread)
        for name in routes:
            removed += await store.drop_thread(name, thread)
    return removed


async def _clear_route_thread(person: Person, erased: set[str], thread: str, caps: Any) -> tuple[int, int] | None:
    """Clear one route thread only ``erased`` wrote in, under its hold; ``(removed, states erased)``.

    Under the hold, the thread's own records decide (:meth:`outside_writer`): a thread a writer
    outside ``erased`` keeps standing is left (``None``) with nothing written to it. Every erase
    sets its mark before it decides on any thread, so of two erases that look at a thread both
    their persons wrote in, the one that looks second sees the other marked and clears it. A thread
    nothing keeps standing is checked for a live intake before the hold and again under it, then
    torn down with the teardown a thread delete runs.
    """
    manager = _require_backend()
    store, persons = manager.records, manager.persons
    if await persons.outside_writer(thread, erased) is None and await store.thread_has_live_intake(thread):
        raise _thread_in_flight(person.person_id, thread)
    async with caps.hold_thread(thread, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        if await persons.outside_writer(thread, erased) is not None:
            return None
        if await store.thread_has_live_intake(thread):
            raise _thread_in_flight(person.person_id, thread)
        return await _tear_down_thread(
            [_route_of(thread)],
            thread,
            reason="person_erased",
            also_targets=((person.target_kind, person.target_name),),
        )


async def _clear_route_threads(
    person: Person, erased: set[str], threads: list[str], caps: Any, visited: list[str]
) -> tuple[int, int]:
    """Clear each of ``threads`` not yet in ``visited``, sorted and one at a time; ``(removed, states erased)``.

    Each thread is appended to ``visited`` once its decision is taken, cleared or left.
    """
    removed = states_erased = 0
    for thread in sorted(set(threads) - set(visited)):
        outcome = await _clear_route_thread(person, erased, thread, caps)
        visited.append(thread)
        if outcome is not None:
            removed += outcome[0]
            states_erased += outcome[1]
    return removed, states_erased


async def _clear_before_teardown(
    person: Person, read: PersonEraseRead, caps: Any, visited: list[str]
) -> tuple[int, int]:
    """Everything the erase clears before the person's own teardown; ``(records removed, state records erased)``.

    The live-intake re-check of the person's own thread (under its FIFO and after the mark: an
    intake admitted while the lock was being taken is refused here, and one after the mark by the
    create), then the aggregated thread of every person it absorbed, then every route thread the
    visit lists of the person and the absorbed ids name, each in sorted order and one thread at a
    time, appended to ``visited``. One more read then visits any thread those lists came to name
    meanwhile (a thread an address of theirs first wrote in during the erase).
    """
    person_id = person.person_id
    if await _require_backend().records.thread_has_live_intake(person_thread_id(person_id)):
        raise ConflictError(_own_in_flight(person_id))
    removed = 0
    for absorbed_id in read.absorbed_ids:
        removed += await _clear_absorbed_thread(person_id, absorbed_id, read.thread_routes[absorbed_id], caps)
    erased = {person_id, *read.absorbed_ids}
    first = await _clear_route_threads(person, erased, read.route_threads, caps, visited)
    again = (await _require_backend().persons.read_for_erase(person_id)).route_threads
    second = await _clear_route_threads(person, erased, again, caps, visited)
    return removed + first[0] + second[0], first[1] + second[1]


async def _erase_gone(person_id: str) -> dict[str, Any]:
    """The erase of an id whose row was gone when the mark was asked, under its FIFO."""
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    gone = await _require_backend().persons.read_for_erase(person_id)
    if gone.absorbed_into is not None:
        raise _absorbed_into_conflict(person_id, gone.absorbed_into)
    # Already erased (a retry) or never a person, with no absorbed-into pointer: reachable from the
    # id alone, its aggregated checkpoint is forgotten regardless — keep-forever memory left behind
    # otherwise — and any async park still addressed to the person or its thread is killed, each run
    # whole. Nothing route-scoped remains to reclaim, and the person's state is erased BEFORE its row
    # on the linked branch, so a missing person means its declared state is already forgotten.
    thread_id = person_thread_id(person_id)
    await cancel_parks_for_thread(thread_id, also_subjects=[(PERSON_KIND, person_id)], reason="person_erased")
    await _delete_thread_checkpoint(thread_id)
    return {"person_id": person_id, "removed": 0, "erased": False, "states_erased": 0, "absorbed_person_ids": []}


async def _erase_marked(person_id: str, caps: Any) -> dict[str, Any]:
    """The erase of a person whose mark this call set, under its FIFO."""
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    manager = _require_backend()
    store, person_store = manager.records, manager.persons
    thread_id = person_thread_id(person_id)
    read = await person_store.read_for_erase(person_id)
    person = read.person
    if person is None:
        raise RuntimeError(
            f"conversations: person {person_id!r} lost its row after its erase mark was set; "
            "retry the erase, its mark stands"
        )
    visited: list[str] = []
    try:
        removed, states_erased = await _clear_before_teardown(person, read, caps, visited)
    except Exception:
        # The person's own thread and its registry entries still stand: give the person back before
        # the refusal. What was cleared stays cleared.
        await person_store.clear_erasing(person_id)
        raise
    # Kill every async park addressed to the person BEFORE its indexes go, so the forget-me does not
    # orphan a parked ``ask`` — each run whole, across the aggregated thread and the person subject,
    # under the same per-thread FIFO.
    await cancel_parks_for_thread(thread_id, also_subjects=[(PERSON_KIND, person_id)], reason="person_erased")
    await _delete_thread_checkpoint(thread_id)
    for name in read.thread_routes[person_id]:
        removed += await store.drop_thread(name, thread_id)
    # The declared-state memory of the person and of every person it absorbed, forgotten BEFORE the
    # person row below, so a failure here leaves the durable marker standing and a retry re-runs the
    # erase; the row delete must be truly last.
    absorbed_ids = read.absorbed_ids
    states_erased += await _erase_person_state(person.target_kind, person.target_name, person_id, absorbed_ids)
    # The row (with its mark, index mappings, absorbed SET and the visit lists of the person and of
    # each absorbed id, refused with a 409 while those lists name a thread this run did not visit)
    # goes LAST, atomically: it is the durable marker a retry finds the owed work by, so an
    # interrupted run re-reads it and finishes. The same step hands each visited thread that still
    # holds a write of theirs, and that no writer outside keeps, to the visit list of that write's
    # door, so the next holder of the address adopts it and that holder's erase clears it.
    erased, _fields = await person_store.erase(person, absorbed_ids, visited)
    return {
        "person_id": person_id,
        "removed": removed,
        "erased": erased,
        "states_erased": states_erased,
        "absorbed_person_ids": absorbed_ids,
    }


@operation(
    summary="Delete a conversation person",
    tags=["conversations"],
    errors=[BadRequestError, ConflictError, NotSupportedError, UnavailableError],
    response_model=PersonDeleteResult,
)
async def delete_conversation_person(person_id: str) -> dict[str, Any]:
    """Erase a LINKED person and every person it absorbed by a link from the bridge's stores.

    - the person's aggregated ``bridge:@person:{person_id}`` thread — its agent checkpoint,
      and across EVERY route the person wrote under (its addresses' routes and the routes its
      thread's writes recorded) its answer records, per-thread transcript indexes, route-thread
      index memberships and per-thread mode override (the thread-delete machinery, reused);
    - for every person it absorbed by a ``/link`` (a chain flattened into it): that person's
      aggregated thread the same way;
    - every route-keyed thread, on any route, that only the person and the persons it absorbed
      wrote in — its checkpoint, records, parks and ``thread``-subject state, exactly as a thread
      delete forgets it. The thread's records decide, read under its hold: a thread a record of
      another writer still stands in (another person, or an address nobody held when it wrote)
      is left to them, unless that person's own erase is running too, and nothing is written to
      a thread left;
    - the declared state of the person and of every person it absorbed (``person``-subject and
      their aggregated threads' ``thread``-subject memory);
    - the person row ``conversations:person:{person_id}``, its absorbed SET, the absorbed
      persons' pointers, their lists of the threads they wrote in, and every ``person_index``
      ``door_address_key → person_id`` mapping of its addresses.

    The bridge's stores only: the interaction store's answered state hashes (which keep a request's
    ``recipient`` and a non-sensitive answer until their idle TTL) and the hook-runs store (whose
    failed run can carry a participant's address in its event payload until an operator retries or
    discards it) are not erased by it.

    Caller authority is the door's grantable ``write`` action — the same grant that forgets a
    thread. IDEMPOTENT and retryable: the erase first sets the person's erase mark under the
    per-thread FIFO, and from then on every person write refuses the person, its addresses are
    attributed as unlinked and its thread takes no new record; the person row is the durable marker
    of an owed erase and is deleted LAST (atomically with its mark, its index mappings, its
    absorbed SET and the lists of the threads they wrote in), so an interruption after the
    teardown began leaves the row and the mark and a retry FINISHES. A refusal before the
    teardown (a 409 or a 503) removes the mark first. A write by the person or a person it absorbed
    that lands in a thread the erase had not reached, in its last moment, is a 409 after the
    teardown: the mark stands, and a retry finishes the erase and clears that thread too. A retry,
    or an id that was never a person, is not a 404 — its aggregated checkpoint is forgotten
    regardless (it defaults to keep-forever) and the call answers ``erased=false``. An id a
    ``/link`` absorbed into another person is a 409 naming that person (``absorbed_into_person_id``), whose
    erase clears it. A turn IN FLIGHT on a thread the erase clears is a 409 (retry once it drains),
    re-checked under the thread's FIFO before its teardown so an admitted turn cannot re-create
    memory behind the erase; a full queue is the retriable 503. As a
    live-caller sync door every acquisition is bounded by ``sync_door_wait_seconds``: a wait past it
    — behind a turn possibly HITL-paused on another worker, behind a turn on an absorbed person's
    thread that holds its lock while it waits for a gate slot, or behind a ``delete-person`` call of
    an absorbed id — is the loud, retriable 503 ``ThreadBusyError`` rather than a block past the
    proxy timeout. A blank ``person_id`` is a 400; no backend is a loud 501.

    Returns ``{"person_id", "removed", "erased", "states_erased", "absorbed_person_ids"}``, where
    ``removed`` counts the answer records this call deleted across the person's routes, in every
    absorbed person's thread and in every route thread it cleared, ``erased`` says whether THIS call
    removed the person row, ``states_erased`` counts the declared-state records forgotten for the person and every
    absorbed person, the cleared route threads' ``thread`` subjects included, and
    ``absorbed_person_ids`` lists, sorted, the absorbed ids this call cleared.

    A send still going out to one of the person's addresses keeps that address on its ``sending``
    row until its completion, end or settle (at most one settle deadline), and ``removed`` does not
    count it. A completion re-attributed once the erase has begun is placed without the person
    link, in the route-keyed thread an inbound from that address would now start.
    """
    if not person_id.strip():
        raise BadRequestError("person_id must be a non-blank person identifier")
    manager = _require_backend()
    from tai42_skeleton.conversations.caps import get_turn_caps

    # Before the FIFO, the early checks only: nothing below uses this read but these refusals.
    early = await manager.persons.read_for_erase(person_id)
    if early.person is None and early.absorbed_into is not None:
        raise _absorbed_into_conflict(person_id, early.absorbed_into)
    if early.person is not None:
        await _check_erase_intake(manager.records, person_id, early)
    thread_id = person_thread_id(person_id)
    caps = get_turn_caps()
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        # The mark decides the branch: set only while the row stands, in one step.
        if not await manager.persons.mark_erasing(person_id):
            return await _erase_gone(person_id)
        return await _erase_marked(person_id, caps)
