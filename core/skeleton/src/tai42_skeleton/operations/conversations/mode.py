"""Per-thread control-mode doors: read the mode in force for a thread and where it comes from,
and set a per-thread override."""

from __future__ import annotations

from typing import Any

from tai42_contract.conversations import CONVERSATION_MODES

from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.operations.response_models_group_a import ThreadModeSetResult, ThreadModeView

from .backend import _mode_store, _require_backend, _require_route
from .models import ThreadModeSet
from .routes import _validate_route_name
from .threads_delete import _thread_delete_routes


@operation(
    summary="Read a conversation thread's control mode",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ThreadModeView,
)
async def get_conversation_thread_mode(route_name: str, thread_id: str) -> dict[str, Any]:
    """The mode in force for ``thread_id`` on ``route_name`` and where it comes from:
    ``{"mode", "source"}``, ``source`` being ``thread`` for a per-thread override and
    ``route`` for the no-override default. A route-keyed thread's default is the route's
    ``initial_mode``; a LINKED person's aggregated thread defaults to ``manual`` when ANY
    route the person spans defaults to ``manual``, else ``agent``.

    The thread-belongs-to-route guard is the thread delete's: a route-keyed id off the route
    is a 400, a person thread off the named route a 404. An unknown route is a loud 404; a
    blank ``thread_id`` a 400."""
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    manager = _require_backend()
    await _require_route(manager, route_name)
    route_names = await _thread_delete_routes(thread_id, route_name)
    override = await _mode_store().get_mode(thread_id)
    if override is not None:
        return {"mode": override, "source": "thread"}
    from tai42_skeleton.conversations.mode import default_mode_for_routes

    return {"mode": await default_mode_for_routes(manager, route_names), "source": "route"}


@operation(
    summary="Set a conversation thread's control mode",
    tags=["conversations"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=ThreadModeSet,
    response_model=ThreadModeSetResult,
)
async def set_conversation_thread_mode(route_name: str, thread_id: str, mode: str) -> dict[str, Any]:
    """Set the per-thread mode override for ``thread_id`` on ``route_name`` to ``mode`` (one
    of ``agent``/``manual``), returning ``{"route_name", "thread_id", "mode", "source"}`` with
    ``source`` always ``thread`` — a set writes an override.

    This is the door an EXTERNAL/programmatic caller names a thread through; an agent flipping
    its OWN live conversation uses the ``set_conversation_mode`` builtin instead, which reads
    the current thread from the turn context rather than naming it.

    Caller authority is the door's grantable ``write`` action. The thread-belongs-to-route
    guard is the thread delete's: a route-keyed id off the route is a 400, a person thread off
    the named route a 404. An unknown route is a loud 404; a blank ``thread_id`` or a ``mode``
    outside the vocabulary is a 400."""
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    if mode not in CONVERSATION_MODES:
        raise BadRequestError(f"mode must be one of {list(CONVERSATION_MODES)}: {mode!r}")
    manager = _require_backend()
    await _require_route(manager, route_name)
    await _thread_delete_routes(thread_id, route_name)
    stored = await _mode_store().set_mode(thread_id, mode)
    return {"route_name": route_name, "thread_id": thread_id, "mode": stored, "source": "thread"}
