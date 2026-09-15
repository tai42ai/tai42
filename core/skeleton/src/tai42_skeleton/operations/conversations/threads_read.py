"""Thread read doors, plus their paging maths and filter parsers.

Route thread listing, single-thread transcript (route-keyed and linked-person aggregate),
route message search and the failed-delivery listing.
"""

from __future__ import annotations

import sys
from typing import Any

from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations._authority import Caller, require_admin
from tai42_skeleton.operations.errors import ForbiddenError, NotSupportedError
from tai42_skeleton.operations.response_models_group_a import (
    FailedConversationsEnvelope,
    MessageSearchEnvelope,
    ThreadSummaryEnvelope,
    TranscriptEnvelope,
)

from .backend import _person_routes, _require_backend, _require_route, _thread_not_found
from .models import (
    MAX_THREAD_PAGE,
    MAX_THREAD_PAGE_SIZE,
    TRANSCRIPT_ORDERS,
    MessageSearchQuery,
    ThreadListQuery,
    TranscriptQuery,
)
from .routes import _validate_route_name

# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``/``_person_store``.
_pkg = sys.modules["tai42_skeleton.operations.conversations"]


def _page_bounds(page: int, page_size: int) -> tuple[int, int]:
    """Return the ``(offset, limit)`` a page/page_size pair names.

    Both must be at least 1 and ``page`` at most :data:`MAX_THREAD_PAGE`; a page size above
    the cap is capped, never refused.
    """
    if page < 1 or page_size < 1:
        raise BadRequestError(f"page and page_size must be >= 1, got page={page} page_size={page_size}")
    if page > MAX_THREAD_PAGE:
        raise BadRequestError(f"page must be <= {MAX_THREAD_PAGE}, got page={page}")
    limit = min(page_size, MAX_THREAD_PAGE_SIZE)
    return (page - 1) * limit, limit


def _next_page(page: int, limit: int, total: int) -> int | None:
    """Return the next page number, or ``None`` on the last page.

    Read from the INDEXED total, not the returned count: a page shortened by rows that
    expired under it is not the end.
    """
    return page + 1 if page * limit < total else None


def _parse_status_filter(status: str | None):
    """Parse the delivery-status set a thread listing filters on, validated against the enum.

    An unknown value is a loud 400, never a silent-ignore. A blank/absent value is no filter.
    """
    if status is None or not status.strip():
        return None
    from tai42_skeleton.conversations.models import DeliveryStatus

    valid = {member.value for member in DeliveryStatus}
    if status not in valid:
        raise BadRequestError(f"status must be one of {sorted(valid)}: {status!r}")
    return frozenset({DeliveryStatus(status)})


def _parse_address_filter(address: str | None) -> str | None:
    """Parse the client-address substring a thread listing filters on.

    A blank/absent value is no filter; otherwise the trimmed substring, matched literally
    against the thread-id suffix.
    """
    if address is None:
        return None
    trimmed = address.strip()
    return trimmed or None


@operation(
    summary="List a conversation route's threads",
    tags=["conversations"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=ThreadListQuery,
    response_model=ThreadSummaryEnvelope,
)
async def list_conversation_threads(
    route_name: str,
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """The threads of ``route_name``, newest activity first, one page at a time.

    A thread listing spans every caller and address on the route, so it is admin-only. Each
    item carries ``thread_id``, ``client_address``, ``message_count``
    and ``last_delivery_status`` summarized from the thread's newest readable record, plus
    ``last_activity_at`` — the route index's own score, which is what this listing SORTS by,
    so the moment shown and the position it is shown in always agree. That score is stamped
    when a record is created and again when its turn completes; a later delivery transition
    on the same record moves the record's ``updated_at`` but not the thread's activity.

    ``status`` (one of the delivery-status vocabulary) keeps only threads whose summary status
    matches; ``address`` keeps only threads whose id client-address suffix contains that
    substring. Neither has a direct index — the per-status indexes are GLOBAL over
    ``message_id``s, not per-route thread ids — so a filter is a BOUNDED app-side post-scan
    and a page that spent its scan budget answers ``truncated: true`` LOUDLY, never a silent
    cut. An unknown ``status`` is a loud 400.

    Authorization is decided BEFORE the route is looked up, so a non-admin is refused the
    same way whether the name routes or not — a 404-here/403-there pair would answer which
    route names exist to a caller with no business knowing. An unknown route is a loud 404
    to an admin; a ``page`` or ``page_size`` below 1, or a ``page`` above the served
    maximum, is a 400. Returns
    ``{"items", "total", "page", "page_size", "next_page", "truncated"}``, where ``total``
    counts the route's indexed threads for an unfiltered listing, or the matches the bounded
    scan found for a filtered one.
    """
    _validate_route_name(route_name)
    manager = _require_backend()
    require_admin(await _pkg.resolve_caller())
    await _require_route(manager, route_name)
    offset, limit = _page_bounds(page, page_size)
    status_filter = _parse_status_filter(status)
    address_filter = _parse_address_filter(address)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    listed = await ConversationRecordStore(ConversationsSettings()).list_route_threads(
        route_name, offset=offset, limit=limit, status=status_filter, address=address_filter
    )
    items = [
        {
            "thread_id": thread.thread_id,
            "client_address": thread.client_address,
            "last_activity_at": thread.last_activity_at,
            "message_count": thread.message_count,
            "last_delivery_status": thread.last_delivery_status.value,
        }
        for thread in listed.threads
    ]
    return {
        "items": items,
        "total": listed.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, listed.total),
        "truncated": listed.truncated,
    }


@operation(
    summary="Read a conversation thread's transcript",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=TranscriptQuery,
    response_model=TranscriptEnvelope,
)
async def get_conversation_thread(
    route_name: str, thread_id: str, page: int = 1, page_size: int = 50, order: str = "asc", q: str | None = None
) -> dict[str, Any]:
    """One thread's records under ``route_name``, one page at a time.

    ``order`` picks the direction: ``asc`` (the default) reads the transcript oldest first,
    ``desc`` reads it newest first, which is the order a live tail wants because page 1 then
    always holds the latest messages. ``page``/``page_size`` window that order from its own
    end, so page 1 of ``desc`` is the newest page and never the oldest.

    ``q`` filters to records whose inbound text or answer contains that substring — a BOUNDED
    scan (the searched text lives inside the record content blob), so a page that spent its
    scan budget answers ``truncated: true`` LOUDLY. A ``q`` read never 404s: the unknown-thread
    404 below is gated on an UNFILTERED read, so under ``q`` an unknown thread and one that
    matched nothing alike read as an EMPTY page.

    An admin reads whole records; a non-admin reads the caller-safe projection, which
    withholds the internal detail of the route key's run. An unknown ``route_name`` is a loud
    404. A thread that is absent or keyed to another route answers the uniform thread
    not-found. A ``page`` or ``page_size`` below 1, a ``page`` above the served maximum, a
    blank ``thread_id`` or an unknown ``order`` is a 400.

    A thread the index still holds but whose rows have expired under the retention TTL is
    NOT that 404: it reads as an empty page carrying the indexed ``total``, until the prune
    pass reclaims the members and the thread becomes unknown.

    Returns ``{"items", "total", "page", "page_size", "next_page", "order", "truncated"}``,
    where ``total`` counts the thread's indexed records for an unfiltered read, or the matches
    a bounded ``q`` scan found.
    """
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    if order not in TRANSCRIPT_ORDERS:
        raise BadRequestError(f"order must be one of {list(TRANSCRIPT_ORDERS)}, got {order!r}")
    offset, limit = _page_bounds(page, page_size)
    needle = q if (q is not None and q.strip()) else None
    manager = _require_backend()
    caller = await _pkg.resolve_caller()
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    if thread_id.startswith(PERSON_THREAD_PREFIX):
        return await _read_person_thread(
            manager, route_name, thread_id, caller, page=page, offset=offset, limit=limit, order=order, q=needle
        )
    await _require_route(manager, route_name)
    transcript = await ConversationRecordStore(ConversationsSettings()).list_thread_records(
        route_name, thread_id, offset=offset, limit=limit, newest_first=order == "desc", q=needle
    )
    # An unknown/expired thread reads as total==0. Under a ``q`` search total is the MATCH
    # count, so an unfiltered read alone turns an empty index into the uniform 404; a search
    # that found nothing in a thread that exists returns an empty page.
    if needle is None and transcript.total == 0:
        raise _thread_not_found(thread_id)
    return _transcript_response(transcript, caller, page=page, limit=limit, order=order)


def _transcript_response(transcript, caller: Caller, *, page: int, limit: int, order: str) -> dict[str, Any]:
    """Build the shared transcript page shape both the route-keyed and aggregated-person reads return.

    An admin reads whole records, a non-admin the caller-safe projection.
    """
    view = (lambda record: record.view()) if caller.is_admin else (lambda record: record.caller_view())
    return {
        "items": [view(record) for record in transcript.records],
        "total": transcript.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, transcript.total),
        "order": order,
        "truncated": transcript.truncated,
    }


async def _read_person_thread(
    manager: BaseConversationsManager,
    route_name: str,
    thread_id: str,
    caller: Caller,
    *,
    page: int,
    offset: int,
    limit: int,
    order: str,
    q: str | None = None,
) -> dict[str, Any]:
    """Return one page of a LINKED person's AGGREGATED transcript, keyed by ``bridge:@person:{person_id}``.

    The merged history across every route the person has written under.
    The supplied ``route_name`` must be one of the person's routes, while the fetch spans the
    indexes of ALL of them. An unknown ``route_name`` is a loud 404; a target mismatch, an
    unknown person, or a route the person never wrote under answers the uniform thread
    not-found. An empty aggregate is that 404 for an unfiltered read; under a ``q`` search it
    is an empty page (the person is real, the search simply matched nothing). An admin reads
    whole records; a non-admin the caller-safe projection.
    """
    person_id = thread_id[len(PERSON_THREAD_PREFIX) :]
    route = await _require_route(manager, route_name)
    person = await _pkg._person_store().get_by_id(person_id)
    if (
        person is None
        or (route.target_kind, route.target_name) != (person.target_kind, person.target_name)
        or route_name not in _person_routes(person)
    ):
        raise _thread_not_found(thread_id)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    transcript = await ConversationRecordStore(ConversationsSettings()).list_person_thread_records(
        sorted(_person_routes(person)), thread_id, offset=offset, limit=limit, newest_first=order == "desc", q=q
    )
    if q is None and transcript.total == 0:
        raise _thread_not_found(thread_id)
    return _transcript_response(transcript, caller, page=page, limit=limit, order=order)


@operation(
    summary="Search a conversation route's messages",
    tags=["conversations"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=MessageSearchQuery,
    response_model=MessageSearchEnvelope,
)
async def search_conversation_messages(route_name: str, q: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Return every record on ``route_name`` whose inbound text or answer contains ``q``, one page at a time.

    Across ALL the route's threads.
    The search spans every caller and address on the route, so it is admin-only, and each item
    is the WHOLE record (the same shape the transcript serves an admin). ``q`` is REQUIRED and
    non-blank. There is no per-route record index, so the search is a BOUNDED nested scan of
    the route's threads and their records; a page that spent its scan budget answers
    ``truncated: true`` LOUDLY, never a silent cut.

    Authorization is decided BEFORE the route is looked up, so a non-admin is refused the same
    way whether the name routes or not. An unknown route is a loud 404 to an admin; a blank
    ``q``, a ``page``/``page_size`` below 1, or a ``page`` above the served maximum, is a 400.
    Returns ``{"items", "total", "page", "page_size", "next_page", "truncated"}``, where
    ``total`` is the matches the bounded scan found.
    """
    _validate_route_name(route_name)
    if not q.strip():
        raise BadRequestError("q must be a non-blank search string")
    manager = _require_backend()
    require_admin(await _pkg.resolve_caller())
    await _require_route(manager, route_name)
    offset, limit = _page_bounds(page, page_size)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    found = await ConversationRecordStore(ConversationsSettings()).search_route_messages(
        route_name, offset=offset, limit=limit, q=q
    )
    return {
        "items": [record.view() for record in found.records],
        "total": found.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, found.total),
        "truncated": found.truncated,
    }


@operation(
    summary="List failed conversation deliveries",
    tags=["conversations"],
    errors=[ForbiddenError, NotSupportedError],
    response_model=FailedConversationsEnvelope,
)
async def list_failed_conversations() -> dict[str, Any]:
    """List every answer record whose delivery ended ``failed``.

    The listing spans every route and caller, so it is admin-only. Returns ``{"items", "total"}``.
    """
    _require_backend()
    require_admin(await _pkg.resolve_caller())
    from tai42_skeleton.conversations.models import DeliveryStatus
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    records = await ConversationRecordStore(ConversationsSettings()).list_by_status(frozenset({DeliveryStatus.FAILED}))
    items = [record.view() for record in records]
    return {"items": items, "total": len(items)}
