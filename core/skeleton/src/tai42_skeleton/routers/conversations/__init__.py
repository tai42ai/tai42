"""HTTP surface for the conversation-route management feature — the authed CRUD doors
the operator and Studio drive over the routing table.

- ``GET /api/conversations`` (AUTHED) — list the stored routes, each with its
  ``callback_secret`` withheld.
- ``GET /api/conversations/{route_name}`` (AUTHED) — read one route by name; an unknown
  name is a loud 404.
- ``POST /api/conversations/{route_name}`` (AUTHED, ``authority_changing``) — create or
  replace a route from a ``ConversationRouteCreate`` body. An ``api`` row's minted
  ``callback_secret`` (present only when the row declares a ``callback_url``) is returned
  ONCE here and never again. Binding the route's
  ``execution_key`` is a pass-role decision the operation takes before any write.
- ``DELETE /api/conversations/{route_name}`` (AUTHED) — delete a route by name; an
  unknown name is a loud 404.
- ``GET /api/conversations/{route_name}/threads`` (AUTHED, admin) — the route's threads,
  newest activity first, paged by ``?page=``/``?pageSize=`` and optionally filtered by
  ``?status=`` (a delivery-status the summary must match) and ``?address=`` (a substring of
  the thread-id client-address suffix). A filter is a bounded post-scan, so the envelope
  carries ``truncated``.
- ``GET /api/conversations/{route_name}/messages/search?q=`` (AUTHED, admin) — every record
  on the route whose inbound text or answer contains ``q``, across all its threads, paged the
  same way. A bounded scan, so the envelope carries ``truncated``.
- ``GET /api/conversations/{route_name}/transcript?thread_id=`` (AUTHED) — one thread's
  transcript, paged the same way, ordered by ``?order=asc|desc`` and optionally filtered by
  ``?q=`` (a bounded scan over the record text, so the envelope carries ``truncated``). The
  thread id is a query value because it holds a percent-encoded principal that no path
  spelling round-trips.
- ``DELETE /api/conversations/{route_name}/thread?thread_id=`` (AUTHED) — forget one
  thread: its agent checkpoint, its answer records and its thread indexes. Forgetting is
  absolute — a valid id on its own route always succeeds, ``removed=0`` when nothing is
  stored, never a 404; a route-keyed id must carry the route's ``bridge:{route_name}:``
  prefix or it is a 400, and a person thread not on the named route is a 404. A turn in
  flight on the thread is a 409. The thread id is a query value, the same as the transcript
  door, because it holds a percent-encoded principal that no path spelling round-trips.
- ``DELETE /api/conversations/persons/{person_id}`` (AUTHED) — erase a linked person
  ENTIRELY: its aggregated ``bridge:@person:{id}`` thread (checkpoint, records, indexes, mode
  override), its person row and every address→person index mapping. Idempotent — an already
  erased person is not a 404; a turn in flight on the aggregated thread is a 409. The person
  id rides the path (a uuid4, so it round-trips a path segment cleanly).
- ``POST /api/conversations/{route_name}/thread/messages`` (AUTHED) — send an operator
  message BY HAND into a thread (no turn runs), delivered as the route identity. Body is
  ``{thread_id, text, address, media?, options?}``; returns ``{message_id, thread_id}``. The
  ``thread_id`` and ``address`` ride the body because the id holds a percent-encoded
  principal; ``media``/``options`` are OPTIONAL richer-send forms delivered with the text.
- ``GET /api/conversations/{route_name}/thread/mode?thread_id=`` (AUTHED) — read a thread's
  control mode and its source (``thread`` override or ``route`` default).
- ``PUT /api/conversations/{route_name}/thread/mode`` (AUTHED) — set a thread's mode override
  from a ``{thread_id, mode}`` body.

The write doors (route create, route delete, thread delete, person delete, operator send,
mode set) share ONE authority: the grantable ``write`` action. The same write grant that
creates a route deletes routes, forgets threads, erases persons, sends operator messages and
sets a thread's mode — no per-thread owner check.

- ``GET /api/conversation-configs`` (AUTHED) — list the per-target conversation configs
  (the ``multichannel`` opt-in + first-contact greeting), keyed ``(target_kind,
  target_name)``.
- ``GET /api/conversation-configs/{target_kind}/{target_name}`` (AUTHED) — read one; an
  unknown key is a loud 404.
- ``PUT /api/conversation-configs/{target_kind}/{target_name}`` (AUTHED) — create or replace
  one from a ``TargetConversationConfig`` body; the target must exist.
- ``DELETE /api/conversation-configs/{target_kind}/{target_name}`` (AUTHED) — delete one; an
  unknown key is a loud 404.

The config doors carry their own ``/api/conversation-configs`` prefix rather than nesting
under ``/api/conversations/{route_name}``, where a ``config`` first segment would collide
with the read-one route door.

Also here: the two authed turn-submission doors (message + event) and the four
conversation-bridge lifecycle hooks. Importing this package registers every route and hook.
Thin adapters over ``tai42_skeleton.operations.conversations`` — no routing logic here.
``get_current_user_id`` and ``reload_gate`` are homed at this package alias so a test's patch
here is the single seam the submission doors read through at call time.
"""

from __future__ import annotations

from tai42_contract.access_control import get_current_user_id

from tai42_skeleton.app.reload_gate import reload_gate

from .extractors import (
    _extract_message_search_query,
    _extract_page_window,
    _extract_paging,
    _extract_person_locale_body,
    _extract_route_create,
    _extract_target_config,
    _extract_thread_delete_query,
    _extract_thread_message,
    _extract_thread_mode_body,
    _extract_thread_mode_query,
    _extract_transcript_query,
)

# The four conversation-bridge lifecycle hooks — importing the module registers them, and
# they are reachable at the package alias for the boot tests that drive them directly.
from .lifecycle import (
    _redrive_pending_conversations,
    _register_conversation_completion_tool,
    _start_conversations_delivery_sweep,
    _stop_conversations_delivery_sweep,
)
from .routes import (
    _delete_conversation_person_op,
    _delete_conversation_thread_op,
    _list_conversation_threads_op,
    _set_conversation_config_op,
    create_conversation_route,
    delete_conversation_config,
    delete_conversation_person,
    delete_conversation_route,
    delete_conversation_thread,
    get_conversation_config,
    get_conversation_message,
    get_conversation_person,
    get_conversation_route,
    get_conversation_thread,
    get_conversation_thread_mode,
    list_conversation_configs,
    list_conversation_routes,
    list_conversation_threads,
    list_failed_conversations,
    search_conversation_messages,
    send_conversation_thread_message,
    set_conversation_config,
    set_conversation_person_locale,
    set_conversation_thread_mode,
)
from .submissions import ConversationTurnAck, send_conversation_event, send_conversation_message

__all__ = [
    "ConversationTurnAck",
    "_delete_conversation_person_op",
    "_delete_conversation_thread_op",
    "_extract_message_search_query",
    "_extract_page_window",
    "_extract_paging",
    "_extract_person_locale_body",
    "_extract_route_create",
    "_extract_target_config",
    "_extract_thread_delete_query",
    "_extract_thread_message",
    "_extract_thread_mode_body",
    "_extract_thread_mode_query",
    "_extract_transcript_query",
    "_list_conversation_threads_op",
    "_redrive_pending_conversations",
    "_register_conversation_completion_tool",
    "_set_conversation_config_op",
    "_start_conversations_delivery_sweep",
    "_stop_conversations_delivery_sweep",
    "create_conversation_route",
    "delete_conversation_config",
    "delete_conversation_person",
    "delete_conversation_route",
    "delete_conversation_thread",
    "get_conversation_config",
    "get_conversation_message",
    "get_conversation_person",
    "get_conversation_route",
    "get_conversation_thread",
    "get_conversation_thread_mode",
    "get_current_user_id",
    "list_conversation_configs",
    "list_conversation_routes",
    "list_conversation_threads",
    "list_failed_conversations",
    "reload_gate",
    "search_conversation_messages",
    "send_conversation_event",
    "send_conversation_message",
    "send_conversation_thread_message",
    "set_conversation_config",
    "set_conversation_person_locale",
    "set_conversation_thread_mode",
]
