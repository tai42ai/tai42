"""Thread and person forget doors.

The absolute, idempotent teardown of a thread's checkpoint, answer records and indexes
(route-keyed or linked-person aggregate), and the whole-person erase.
"""

from __future__ import annotations

import sys
from typing import Any

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import ConflictError, NotSupportedError, UnavailableError
from tai42_skeleton.operations.response_models_group_a import PersonDeleteResult, ThreadDeleteResult

from .backend import _person_routes, _require_backend, _thread_not_found
from .models import ThreadDeleteQuery
from .routes import _validate_route_name

# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``/``_person_store``.
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
    every route index carrying it is reclaimed (else a member strands under one). Caller
    authority is the door's grantable write action — the same grant that creates a route —
    never derived here.
    """
    if not thread_id.startswith(PERSON_THREAD_PREFIX):
        prefix = f"{BRIDGE_THREAD_PREFIX}{route_name}:"
        if not thread_id.startswith(prefix):
            raise BadRequestError(
                f"thread_id {thread_id!r} is not a thread of route {route_name!r}: it must start with {prefix!r}"
            )
        return [route_name]
    person = await _pkg._person_store().get_by_id(thread_id[len(PERSON_THREAD_PREFIX) :])
    if person is None or route_name not in _person_routes(person):
        raise _thread_not_found(thread_id)
    return sorted(_person_routes(person))


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

    Returns ``{"removed", "route_name", "thread_id"}``, where ``removed`` counts the answer
    records this call deleted (0 when a prior run already cleared them, when their rows had
    expired under the retention TTL, or when the id was never stored).
    """
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    _require_backend()
    from tai42_skeleton.conversations.caps import get_turn_caps
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    store = ConversationRecordStore(ConversationsSettings())
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
        # Cancel every async ``ask_user`` parked on this thread BEFORE the indexes go, so
        # the deletion does not orphan a park (its expiry reaper would later fire a
        # continuation into this now-deleted thread — a delivery retry storm — and its
        # channel correlation would mute the participant's number until the deadline). Runs under
        # the same per-thread FIFO the teardown holds; idempotent, so a retry re-runs it.
        from tai42_skeleton.interactions.helper import cancel_parks_for_thread

        await cancel_parks_for_thread(thread_id)
        await _delete_thread_checkpoint(thread_id)
        removed = 0
        for name in route_names:
            removed += await store.drop_thread(name, thread_id)
    return {"removed": removed, "route_name": route_name, "thread_id": thread_id}


@operation(
    summary="Delete a conversation person",
    tags=["conversations"],
    errors=[BadRequestError, ConflictError, NotSupportedError, UnavailableError],
    response_model=PersonDeleteResult,
)
async def delete_conversation_person(person_id: str) -> dict[str, Any]:
    """Erase a LINKED person ENTIRELY, forgetting every store that names it.

    - the person's aggregated ``bridge:@person:{person_id}`` thread — its agent checkpoint,
      and across EVERY route the person wrote under its answer records, per-thread transcript
      indexes, route-thread index memberships and per-thread mode override (the thread-delete
      machinery, reused);
    - the person row ``conversations:person:{person_id}``;
    - every ``person_index`` ``door_address_key → person_id`` mapping of its addresses.

    Caller authority is the door's grantable ``write`` action — the same grant that forgets a
    thread. IDEMPOTENT and retryable: the person row is the durable marker of an owed erase and
    is deleted LAST (atomically with its index mappings), so an interruption re-reads it and a
    retry FINISHES. A person that is already gone (a retry, or one that never existed) is not a
    404 — its aggregated checkpoint is forgotten regardless (it defaults to keep-forever) and
    the call answers ``erased=false``. A turn IN FLIGHT on the aggregated thread is a 409
    (retry once it drains), re-checked under the per-thread FIFO before any teardown so an
    admitted turn cannot re-create memory behind the erase; a full queue is the retriable 503.
    As a live-caller sync door the acquisition is bounded by ``sync_door_wait_seconds`` (both the
    linked and the already-gone branch): a wait past it — behind a turn possibly HITL-paused on
    another worker — is the loud, retriable 503 ``ThreadBusyError`` rather than a block past the
    proxy timeout. A blank ``person_id`` is a 400; no backend is a loud 501.

    Returns ``{"person_id", "removed", "erased"}``, where ``removed`` counts the answer records
    this call deleted across the person's routes and ``erased`` says whether THIS call removed
    the person row.
    """
    if not person_id.strip():
        raise BadRequestError("person_id must be a non-blank person identifier")
    _require_backend()
    from tai42_skeleton.conversations.caps import get_turn_caps
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    store = ConversationRecordStore(ConversationsSettings())
    person_store = _pkg._person_store()
    thread_id = f"{PERSON_THREAD_PREFIX}{person_id}"
    person = await person_store.get_by_id(person_id)
    caps = get_turn_caps()
    if person is None:
        # Already erased (a retry) or never a person: reachable from the id alone, its
        # aggregated checkpoint is forgotten regardless — keep-forever memory left behind
        # otherwise — and nothing route-scoped remains to reclaim. The checkpoint delete runs
        # under the per-thread FIFO and cross-worker lease, exactly as the linked branch does,
        # so an in-flight turn on the aggregated thread cannot re-fork the memory behind it.
        caps.reserve_thread_slot(thread_id)
        async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
            # Cancel any async park still bound to the aggregated thread even on the
            # already-gone branch: the checkpoint is forgotten regardless, so a lingering
            # park would otherwise be orphaned exactly as on the linked branch.
            from tai42_skeleton.interactions.helper import cancel_parks_for_thread

            await cancel_parks_for_thread(thread_id)
            await _delete_thread_checkpoint(thread_id)
        return {"person_id": person_id, "removed": 0, "erased": False}
    route_names = sorted(_person_routes(person))
    in_flight_409 = f"conversation person {person_id!r} has a turn in flight; retry once it drains"
    if await store.thread_has_live_intake(thread_id):
        raise ConflictError(in_flight_409)
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        # Re-check UNDER the FIFO, exactly as the thread delete does: an intake admitted while
        # the lock was being taken is refused before any teardown, never left to re-stamp a
        # route index behind the erase.
        if await store.thread_has_live_intake(thread_id):
            raise ConflictError(in_flight_409)
        # Cancel every async park on the aggregated thread BEFORE its indexes go, so the
        # forget-me does not orphan a parked ``ask_user`` — the same cascade the thread
        # delete runs, under the same per-thread FIFO.
        from tai42_skeleton.interactions.helper import cancel_parks_for_thread

        await cancel_parks_for_thread(thread_id)
        await _delete_thread_checkpoint(thread_id)
        removed = 0
        for name in route_names:
            removed += await store.drop_thread(name, thread_id)
        # The row (with its index mappings) goes LAST, atomically: it is the durable marker a
        # retry finds the owed work by, so an interrupted run re-reads it and finishes.
        erased, _fields = await person_store.erase(person)
    return {"person_id": person_id, "removed": removed, "erased": erased}
