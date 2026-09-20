"""Thin operation-adapter registrations for the conversation route doors.

Covers the route CRUD, thread, person and config doors.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.conversations import create_conversation_route as _create_conversation_route_op
from tai42_skeleton.operations.conversations import delete_conversation_config as _delete_conversation_config_op
from tai42_skeleton.operations.conversations import delete_conversation_person as _delete_conversation_person_op
from tai42_skeleton.operations.conversations import delete_conversation_route as _delete_conversation_route_op
from tai42_skeleton.operations.conversations import delete_conversation_thread as _delete_conversation_thread_op
from tai42_skeleton.operations.conversations import get_conversation_config as _get_conversation_config_op
from tai42_skeleton.operations.conversations import get_conversation_message as _get_conversation_message_op
from tai42_skeleton.operations.conversations import get_conversation_person as _get_conversation_person_op
from tai42_skeleton.operations.conversations import get_conversation_route as _get_conversation_route_op
from tai42_skeleton.operations.conversations import get_conversation_thread as _get_conversation_thread_op
from tai42_skeleton.operations.conversations import get_conversation_thread_mode as _get_conversation_thread_mode_op
from tai42_skeleton.operations.conversations import list_conversation_configs as _list_conversation_configs_op
from tai42_skeleton.operations.conversations import list_conversation_routes as _list_conversation_routes_op
from tai42_skeleton.operations.conversations import list_conversation_threads as _list_conversation_threads_op
from tai42_skeleton.operations.conversations import list_failed_conversations as _list_failed_conversations_op
from tai42_skeleton.operations.conversations import search_conversation_messages as _search_conversation_messages_op
from tai42_skeleton.operations.conversations import (
    send_conversation_thread_message as _send_conversation_thread_message_op,
)
from tai42_skeleton.operations.conversations import set_conversation_config as _set_conversation_config_op
from tai42_skeleton.operations.conversations import set_conversation_person_locale as _set_conversation_person_locale_op
from tai42_skeleton.operations.conversations import set_conversation_thread_mode as _set_conversation_thread_mode_op

from .extractors import (
    _extract_message_search_query,
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

list_conversation_routes = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_conversation_routes_op),
    path="/api/conversations",
    method="GET",
    action="read",
)

get_conversation_route = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_route_op),
    path="/api/conversations/{route_name}",
    method="GET",
    action="read",
)

create_conversation_route = register_operation_route(
    tai42_app,
    operation_metadata_of(_create_conversation_route_op),
    path="/api/conversations/{route_name}",
    method="POST",
    context_extractor=_extract_route_create,
    action="write",
)

delete_conversation_route = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_route_op),
    path="/api/conversations/{route_name}",
    method="DELETE",
    action="write",
)


# The admin-tier failed-delivery listing and the route message search sit on literal paths so
# the ``{route_name}`` get/delete doors above never capture them. Both are registered BEFORE
# the read-one door — the message search on ``messages/search`` so ``search`` is never captured
# as a ``{message_id}`` — for the same reason they read a literal segment.
list_failed_conversations = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_failed_conversations_op),
    path="/api/conversations/messages/failed",
    method="GET",
    action="read",
)

search_conversation_messages = register_operation_route(
    tai42_app,
    operation_metadata_of(_search_conversation_messages_op),
    path="/api/conversations/{route_name}/messages/search",
    method="GET",
    context_extractor=_extract_message_search_query,
    action="read",
)

get_conversation_message = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_message_op),
    path="/api/conversations/{route_name}/messages/{message_id}",
    method="GET",
    action="read",
)

list_conversation_threads = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_conversation_threads_op),
    path="/api/conversations/{route_name}/threads",
    method="GET",
    context_extractor=_extract_paging,
    action="read",
)

get_conversation_thread = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_thread_op),
    path="/api/conversations/{route_name}/transcript",
    method="GET",
    context_extractor=_extract_transcript_query,
    action="read",
)

# The item-level thread delete addresses the thread by ``?thread_id=`` query, the same as
# the transcript read door, because the id holds a percent-encoded principal that no path
# spelling round-trips. Its literal ``thread`` segment keeps it clear of the ``{route_name}``
# read/delete doors above.
delete_conversation_thread = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_thread_op),
    path="/api/conversations/{route_name}/thread",
    method="DELETE",
    context_extractor=_extract_thread_delete_query,
    action="write",
)

# Erasing a linked PERSON — its aggregated thread, its person row and every address→person
# index mapping — sits on its own ``persons/{person_id}`` literal first segment (``persons``
# can never be a route slug), clear of the ``{route_name}`` doors. It carries the SAME write
# action as the thread delete: the person id rides the path (a uuid4, no percent-encoded
# principal, so unlike a thread id it round-trips a path segment cleanly).
delete_conversation_person = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_person_op),
    path="/api/conversations/persons/{person_id}",
    method="DELETE",
    action="write",
)

get_conversation_person = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_person_op),
    path="/api/conversations/persons/{person_id}",
    method="GET",
    action="read",
)

set_conversation_person_locale = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_conversation_person_locale_op),
    path="/api/conversations/persons/{person_id}/locale",
    method="PUT",
    context_extractor=_extract_person_locale_body,
    action="write",
)

# The operator-send and mode doors sit under the same literal ``thread`` segment as the
# thread delete, one level deeper (``/thread/messages``, ``/thread/mode``), so they never
# capture the ``{route_name}`` read/delete doors above.
send_conversation_thread_message = register_operation_route(
    tai42_app,
    operation_metadata_of(_send_conversation_thread_message_op),
    path="/api/conversations/{route_name}/thread/messages",
    method="POST",
    context_extractor=_extract_thread_message,
    action="write",
)

get_conversation_thread_mode = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_thread_mode_op),
    path="/api/conversations/{route_name}/thread/mode",
    method="GET",
    context_extractor=_extract_thread_mode_query,
    action="read",
)

set_conversation_thread_mode = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_conversation_thread_mode_op),
    path="/api/conversations/{route_name}/thread/mode",
    method="PUT",
    context_extractor=_extract_thread_mode_body,
    action="write",
)

list_conversation_configs = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_conversation_configs_op),
    path="/api/conversation-configs",
    method="GET",
    action="read",
)

get_conversation_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_conversation_config_op),
    path="/api/conversation-configs/{target_kind}/{target_name}",
    method="GET",
    action="read",
)

set_conversation_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_conversation_config_op),
    path="/api/conversation-configs/{target_kind}/{target_name}",
    method="PUT",
    context_extractor=_extract_target_config,
    action="write",
)

delete_conversation_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_conversation_config_op),
    path="/api/conversation-configs/{target_kind}/{target_name}",
    method="DELETE",
    action="write",
)
