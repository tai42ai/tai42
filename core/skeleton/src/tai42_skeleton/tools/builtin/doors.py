"""The builtin conversation-door tools: a deployment self-calls its own keyless doors under the run's identity.

Two tools, one per platform door that runs its turn AS the ROUTE's own execution key, so an
in-process caller needs no key: the MESSAGE door (:func:`send_conversation_message`) and the
EVENT door (:func:`send_conversation_event`). Each is the in-process face of an HTTP door that
also serves external callers — a DISTINCT surface: the deployment self-submitting under its own
identity, with no HTTP request and no route key handed to the flow.

Each tool (a) refuses a run with no accountable execution identity, (b) applies the door route's
OWN authorization — ``authz.check`` over the door route's registered template, the same decision
the HTTP edge runs — and only then (c) calls the door in-process. The turn's authority stays the
route's execution key; the run's execution identity is only the accountable principal the door
buckets on and records. The doors submit with no live client and no sync wait, so a tool returns
the accepted receipt at once and the turn's answer is delivered by the ROUTE's own configured
delivery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.access_control.projection import NO_AUTH_USER_ID
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.authz import check
from tai42_skeleton.authz.execution_identity import get_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations.turn.errors import UnauthenticatedApiCallerError
from tai42_skeleton.operations.registry import OperationMetadata


async def _never_connected() -> bool:
    """The client-connected probe an in-process caller presents.

    A self-call has no live socket, so the probe reads ``False`` always: the door never
    claims a finished turn's inline answer and returns its accepted receipt at once, and the
    answer is delivered by the route's own configured delivery (an api route's signed
    callback or its poll record, a channel-target event's channel).
    """
    return False


def _accountable_principal(identity: CallerIdentity | None) -> str | None:
    """The accountable principal an in-process door self-call records, resolved as the HTTP door resolves it.

    The run's bound execution identity supplies the id under an enabled gate; with the gate
    off no identity is bound and the platform's synthetic no-auth principal stands in — the
    same principal the HTTP door's own resolution names. ``None`` is returned only under an
    enabled gate with no identity bound, the one state the door tool refuses before it
    reaches the door.
    """
    principal = identity.user_id if identity is not None else None
    if principal is not None and principal.strip():
        return principal
    if not access_control_settings().enable:
        return NO_AUTH_USER_ID
    return None


def _door_operation_metadata(handler: Callable[..., Awaitable[object]]) -> OperationMetadata:
    """The door route's operation metadata for the authz check, built from its live registration.

    The doors are custom routes, not operations, so no operation record exists to import: the
    registered template and method are read off the route registry by the handler's own
    registered name. ``authz.check`` then synthesizes the concrete path from that template
    plus ``{"route_name": ...}``, so the gate pins the SAME route the HTTP edge serves —
    never a path literal that could drift from the registered route.
    """
    name = handler.__name__
    matches = [meta for meta in route_registry.routes() if meta.name == name and not meta.mounted]
    if len(matches) != 1:
        raise RuntimeError(
            f"conversation door {name!r} does not resolve to exactly one registered route "
            f"({len(matches)} found); the door tool cannot authorize a route it cannot pin"
        )
    route = matches[0]
    method = next((served for served in route.methods if served != "HEAD"), None)
    if method is None:
        raise RuntimeError(f"conversation door route {route.path!r} declares no non-HEAD method to authorize")
    return OperationMetadata(
        name=route.name,
        func=handler,
        summary=route.summary,
        route_template=route.path,
        http_method=method,
    )


async def _authorize_door(route_name: str, handler: Callable[..., Awaitable[object]]) -> str:
    """Refuse a run with no accountable identity, apply the door route's gate, and return the principal to submit under.

    The gate is ``authz.check`` over the door route's registered template — the SAME decision
    the HTTP edge applies before its handler runs (owner-attenuated effective scopes, the
    identity's jq conditions, the per-tag level, a deleted/narrowed principal): a miss raises
    :class:`~tai42_skeleton.operations.errors.PermissionDeniedError` and the door never runs.
    A run with no bound execution identity under an enabled gate carries no accountable
    principal and is refused as an unauthenticated caller before the check, since the check
    needs a bound identity to gate on.
    """
    identity = get_execution_identity()
    principal = _accountable_principal(identity)
    if principal is None:
        raise UnauthenticatedApiCallerError(
            f"conversation route {route_name!r} needs an accountable execution identity and this run has none bound"
        )
    # ``check`` is the route gate under an enabled gate — where an identity is always bound
    # (a missing one refused above). With the gate off no identity is bound and the gate
    # admits everything, exactly as the HTTP edge runs no middleware.
    if identity is not None:
        await check(identity, _door_operation_metadata(handler), {"route_name": route_name})
    return principal


@tai42_app.tools.tool(tags={"conversations"})
async def send_conversation_message(
    route_name: str,
    external_user_id: str,
    text: str,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
    locale: str | None = None,
) -> dict[str, str]:
    """Send a message to a conversation route from inside a run, under the deployment's own identity.

    The message enters ``route_name``'s conversation as a turn run AS the route's execution
    key — no key is handed to the run. The run's execution identity is the accountable
    principal the turn buckets on and records; the SAME authorization the HTTP door applies
    to an external sender is applied to this run before the door runs. The turn's answer is
    delivered by the route's own configured delivery (its signed callback or its poll
    record), never inline.

    Args:
        route_name: The api conversation route to send to.
        external_user_id: The caller's handle for the end user; it keys the thread and is the
            address the answer is delivered against.
        text: The message text (non-blank).
        params: Opaque caller-supplied entry parameters delivered to a tool target's payload
            under ``params``; the platform attaches no meaning and no trust.
        form: A structured participant submission riding with ``text``; opaque and untrusted.
        attachments: Structured media sent with ``text`` (image/document/video/audio).
        location: A geographic point shared with ``text``.
        locale: The end user's BCP 47 language tag (e.g. ``he-IL``); ``None`` supplies none.

    Returns:
        The accepted receipt ``{"message_id", "thread_id"}``.

    Raises:
        UnauthenticatedApiCallerError: The run has no accountable execution identity bound
            under an enabled gate.
        PermissionDeniedError: The run's identity is not authorized to send to ``route_name``.
        ConversationRouteResolutionError: No api conversation route named ``route_name``.
        AddressRateLimitedError: The route's per-principal rate cap is exceeded.
    """
    from tai42_skeleton.conversations import submit_api_message
    from tai42_skeleton.routers.conversations import send_conversation_message as _door

    principal = await _authorize_door(route_name, _door)
    result = await submit_api_message(
        route_name,
        external_user_id,
        text,
        principal,
        0,
        params=params,
        form=form,
        attachments=attachments,
        location=location,
        locale=locale,
        client_connected=_never_connected,
    )
    return {"message_id": result.message_id, "thread_id": result.thread_id}


@tai42_app.tools.tool(tags={"conversations"})
async def send_conversation_event(
    route_name: str,
    event: ConversationEvent,
    thread_id: str | None = None,
    address: str | None = None,
) -> dict[str, str]:
    """Deliver a structured event to an existing thread of a conversation route, under the deployment's own identity.

    The event enters an EXISTING thread of ``route_name`` as a turn run AS the route's TOOL
    target under the route's execution key — no key is handed to the run. Address the thread
    by exactly one of ``thread_id`` or ``address``. The run's execution identity is the
    accountable principal the event records as its authorizer; the SAME authorization the
    HTTP event door applies is applied to this run before the door runs — an authorized
    writer may address any existing thread of the route. The event never mints a thread and
    never runs an agent target; its answer is delivered by the target route's own door.

    Args:
        route_name: The conversation route the target thread belongs to.
        event: The structured event (its ``event_id`` is the idempotency key).
        thread_id: The id of the existing thread to deliver to; give this OR ``address``.
        address: The client address of the existing thread to deliver to; give this OR
            ``thread_id``.

    Returns:
        The accepted receipt ``{"message_id", "thread_id"}``; a redelivery of the same
        ``event.event_id`` returns the original turn's ``message_id`` and runs no second turn.

    Raises:
        UnauthenticatedApiCallerError: The run has no accountable execution identity bound
            under an enabled gate.
        PermissionDeniedError: The run's identity is not authorized to send to ``route_name``.
        ConversationRouteResolutionError: No conversation route named ``route_name``.
        EventTargetNotToolError: The route targets an agent; an event runs only a tool target.
        ThreadNotFoundError: No such thread on the route (an event never mints a thread).
        AddressRateLimitedError: The route's per-principal rate cap is exceeded.
    """
    from tai42_skeleton.conversations import submit_event
    from tai42_skeleton.routers.conversations import send_conversation_event as _door

    principal = await _authorize_door(route_name, _door)
    submission = ConversationEventSubmission(event=event, thread_id=thread_id, address=address, wait_seconds=0)
    result = await submit_event(route_name, submission, principal, client_connected=_never_connected)
    return {"message_id": result.message_id, "thread_id": result.thread_id}
