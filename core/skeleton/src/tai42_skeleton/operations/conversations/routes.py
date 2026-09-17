"""Conversation-route CRUD doors and their body validators.

Create/upsert (pass-role bind + target-exists + jq compile + callback-secret mint),
read/list (secret withheld), delete (with thread-index reclamation), and the single
answer-record read.
"""

from __future__ import annotations

import secrets
import sys
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.conversations import ROUTE_NAME_RE, ConversationRoute, ConversationRouteCreate, OverlapPolicy
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data import get_compiled_jq

from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefusedError,
)
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import ForbiddenError, NotSupportedError, ValidationRejectedError
from tai42_skeleton.operations.response_models_group_a import (
    ConversationRecordView,
    ConversationRouteCreateResult,
    ConversationRouteListEnvelope,
    ConversationRouteView,
    RouteRemoveResult,
)
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError

from .backend import _require_backend

# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``/``_person_store``.
_pkg = sys.modules["tai42_skeleton.operations.conversations"]


def _public_route_view(route: ConversationRoute) -> dict[str, Any]:
    """A stored row for a read response, its ``callback_secret`` stripped."""
    data = route.model_dump(mode="json")
    data.pop("callback_secret", None)
    return data


def _validate_route_name(route_name: str) -> None:
    if not ROUTE_NAME_RE.fullmatch(route_name):
        raise BadRequestError(f"route_name must be a slug matching {ROUTE_NAME_RE.pattern!r}: {route_name!r}")


async def _assert_target_exists(target_kind: str, target_name: str) -> None:
    """Existence only — the key's authority over the target is not checked here.

    The turn runs as the key, so authority is deliberately not checked here. An ``agent``
    target must be registered; a ``tool`` target must resolve on the live tool registry.
    A miss is a loud 404, mirroring either side.
    """
    from tai42_skeleton.app import instance
    from tai42_skeleton.tools.binding import UnknownToolError

    if target_kind == "agent":
        if target_name not in instance.app.agents.all_agents():
            raise NotFoundError(f"agent not found: {target_name!r}")
        return
    try:
        await instance.app.tools.get_tool(target_name)
    except UnknownToolError as exc:
        raise NotFoundError(f"tool not found: {target_name!r}") from exc


async def _assert_target_bindable(target_kind: str, target_name: str) -> None:
    """Consult the registered bind validator for the target's kind, once the target exists.

    A plugin registers a validator through ``app.conversations.register_target_validator``;
    a validator returning message lines refuses the route with them (a 422), so a defect
    the target carries — reading a state no binding supplies — is caught at bind, never
    deferred to run time. No validator for the kind leaves the create unchanged.
    """
    from tai42_skeleton.app import instance

    validator = instance.app.conversations.target_validator(target_kind)
    if validator is None:
        return
    messages = await validator(target_name)
    if messages:
        raise ValidationRejectedError("\n".join(messages))


async def _assert_exprs_compile(create: ConversationRouteCreate) -> None:
    """Render a tool target's templated jq programs and compile each at create.

    An invalid one is refused here, not at the first message. A by-id text whose stored
    resource cannot be fetched fails the create loudly, naming the field and the id. The
    model already forbids exprs on an ``agent`` target.
    """
    for field, text in (("payload_expr", create.payload_expr), ("reply_expr", create.reply_expr)):
        if text is None:
            continue
        try:
            program = await tai42_app.storage.resource_manager.render_templated_text(text)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise BadRequestError(
                f"{field} references stored id {text.id!r}, which could not be fetched: {exc}"
            ) from exc
        try:
            get_compiled_jq(program)
        except Exception as exc:
            raise BadRequestError(f"invalid {field}: {exc}") from exc


def _door_flip_refusal(refused: DoorFlipRefusedError) -> BadRequestError:
    """The operator-facing refusal for an edit that would change a route's ``door`` while it holds threads.

    The two doors key their threads differently — an api thread names its owning caller
    principal in its own id, a channel thread names the medium's address — so the held
    threads cannot be re-keyed under the new door. The flip is refused rather than
    half-applied, and refused by the write itself so a first message opening a thread cannot
    slip in behind a separate check.
    """
    return BadRequestError(
        f"conversation route {refused.route_name!r} holds {refused.held} thread(s) opened on its "
        f"{refused.from_door!r} door and cannot be changed to {refused.to_door!r}: delete the route "
        "(which reclaims its threads) and create it again under the new door, or create the new door "
        "under a different route_name"
    )


async def _unclaimed_channel_identity(
    manager: BaseConversationsManager, *, route_name: str, channel: str, our_identity: str
) -> str:
    """The canonical ``our_identity`` a ``channel`` row is STORED under.

    Refused when another route already claims that ``(channel, identity)`` pair. Inbound
    routing matches the canonical form, so a second claimant would make every message to
    that identity unresolvable; it is refused here at the write instead.
    """
    try:
        identity = canonical_address(our_identity)
    except ValueError as exc:
        raise BadRequestError(f"invalid our_identity: {exc}") from exc
    for row in (await manager.list_routes()).values():
        if (
            row.route_name != route_name
            and row.door == "channel"
            and row.channel == channel
            and row.our_identity is not None
            and canonical_address(row.our_identity) == identity
        ):
            raise BadRequestError(f"channel {channel!r} identity {identity!r} is already routed by {row.route_name!r}")
    return identity


@operation(
    summary="List conversation routes",
    tags=["conversations"],
    errors=[NotSupportedError],
    response_model=ConversationRouteListEnvelope,
)
async def list_conversation_routes() -> dict[str, Any]:
    """Every stored conversation route, each with its ``callback_secret`` withheld.

    Returns ``{"items", "total"}``.
    """
    manager = _require_backend()
    routes = await manager.list_routes()
    items = [_public_route_view(route) for route in routes.values()]
    return {"items": items, "total": len(items)}


@operation(
    summary="Get a conversation route",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ConversationRouteView,
)
async def get_conversation_route(route_name: str) -> dict[str, Any]:
    """One conversation route by name, with its ``callback_secret`` withheld.

    An unknown name is a loud 404; a name that is not a valid slug is a 400.
    """
    _validate_route_name(route_name)
    manager = _require_backend()
    route = await manager.get_route(route_name)
    if route is None:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    return _public_route_view(route)


@operation(
    summary="Create or replace a conversation route",
    tags=["conversations"],
    destructive=True,
    authority_changing=True,
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError, ValidationRejectedError],
    request_model=ConversationRouteCreate,
    response_model=ConversationRouteCreateResult,
)
async def create_conversation_route(
    route_name: str,
    door: str,
    target_kind: str,
    target_name: str,
    execution_key: str,
    payload_expr: TemplatedText | None = None,
    reply_expr: TemplatedText | None = None,
    initial_mode: str = "agent",
    channel: str | None = None,
    our_identity: str | None = None,
    callback_url: str | None = None,
    turns_per_hour_override: int | None = None,
    error_reply_text: str | None = None,
    locale: str | None = None,
    overlap: OverlapPolicy | None = None,
) -> dict[str, Any]:
    """Create a conversation route from its flat parameters — an UPSERT (create AND edit path).

    ``initial_mode`` is the route's default control mode (``agent`` runs the turn, ``manual``
    suppresses it for an operator to answer) when a thread carries no per-thread override.

    ``execution_key`` is the api-key identity the turn runs AS; the caller must be allowed
    to delegate it and it must be usable by a tokenless fire, both decided BEFORE the write
    so a refusal leaves any existing row untouched. ``target_name`` must merely EXIST — the
    agent (``target_kind=agent``) or tool (``target_kind=tool``) — the key's live grants
    bound the turn at fire. A tool target's ``payload_expr``/``reply_expr`` templated jq
    programs, when given, are rendered and compiled here so an invalid one — or a by-id text
    whose stored resource cannot be fetched — is refused at create, not at first message. A
    ``channel`` row's ``our_identity`` is stored canonicalized and must not already be routed
    on that channel. An edit that would change the ``door`` of a route already HOLDING
    threads is refused: the two doors key their threads differently, so the held threads
    cannot be re-keyed under the new door.
    An ``api`` row that declares a ``callback_url`` has its ``callback_secret`` minted here
    and returned ONCE; a poll-only api row (no callback) mints none and reads its answers
    back from the poll door. A positive
    ``turns_per_hour_override`` runs this route's per-address buckets at that rate instead
    of the global ``per_address_turns_per_hour`` cap; ``None`` runs them at the global rate.
    A non-blank ``error_reply_text`` is the participant-facing reply sent when a turn on this route
    fails; ``None`` uses the built-in default.
    A ``locale`` is the operator-declared default language templated reply parts render in when
    a turn supplies none; it is the last fallback under a per-turn or stored-contact locale and
    is stored canonicalized. ``None`` declares no route default (bare/English).
    An ``overlap`` policy governs how a running turn treats the newer participant messages that
    overlap it (learn/cancel/carry); ``None`` stores the default policy (continue/one/no window),
    which runs one turn per message and leaves the payload unchanged.
    Returns ``{"created", "route_name", "route", "callback_secret"}``.
    """
    # Validate the whole body shape at the operation, not the edge: the MCP tool and a
    # direct call take these flat parameters and bypass the HTTP extractor.
    try:
        create = ConversationRouteCreate(
            route_name=route_name,
            door=door,  # pyright: ignore[reportArgumentType]
            target_kind=target_kind,  # pyright: ignore[reportArgumentType]
            target_name=target_name,
            payload_expr=payload_expr,
            reply_expr=reply_expr,
            initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
            execution_key=execution_key,
            channel=channel,
            our_identity=our_identity,
            callback_url=callback_url,
            turns_per_hour_override=turns_per_hour_override,
            error_reply_text=error_reply_text,
            locale=locale,
            overlap=overlap if overlap is not None else OverlapPolicy(),
        )
    except ValueError as exc:
        raise BadRequestError(f"invalid conversation route: {exc}") from exc

    manager = _require_backend()

    await _assert_target_exists(create.target_kind, create.target_name)
    await _assert_target_bindable(create.target_kind, create.target_name)
    await _assert_exprs_compile(create)

    stored = create.model_dump()
    # Only a ``channel`` row carries both fields; its identity is stored canonicalized.
    if create.channel is not None and create.our_identity is not None:
        stored["our_identity"] = await _unclaimed_channel_identity(
            manager, route_name=create.route_name, channel=create.channel, our_identity=create.our_identity
        )

    execution_key_fingerprint = await _pkg.assert_execution_key_bindable(
        await _pkg.resolve_caller(), create.execution_key
    )

    # Signs the api-door callback; a ``channel`` row and a poll-only api row (no callback
    # declared) sign nothing and carry no secret.
    callback_secret = secrets.token_urlsafe(32) if create.door == "api" and create.callback_url is not None else None

    route = ConversationRoute(
        **stored,
        callback_secret=callback_secret,
        execution_key_fingerprint=execution_key_fingerprint,
    )
    try:
        created = await manager.put_route(route)
    except DoorFlipRefusedError as refused:
        raise _door_flip_refusal(refused) from refused
    return {
        "created": created,
        "route_name": route.route_name,
        "route": _public_route_view(route),
        "callback_secret": callback_secret,
    }


@operation(
    summary="Read one conversation answer record",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ConversationRecordView,
)
async def get_conversation_message(route_name: str, message_id: str) -> dict[str, Any]:
    """One conversation answer record by ``message_id`` under ``route_name``.

    A missing record or one on another route is a 404. An admin reads the whole record; a
    non-admin reads the caller-safe projection, which withholds the internal detail of the
    route key's run.
    """
    _validate_route_name(route_name)
    _require_backend()
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    record = await ConversationRecordStore(ConversationsSettings()).get_record(message_id)
    if record is None or record.route_name != route_name:
        raise NotFoundError(f"conversation record not found: {message_id!r}")
    caller = await _pkg.resolve_caller()
    if caller.is_admin:
        return record.view()
    return record.caller_view()


@operation(
    summary="Delete a conversation route",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=RouteRemoveResult,
)
async def delete_conversation_route(route_name: str) -> dict[str, Any]:
    """Delete a conversation route by name, along with the thread indexes it owned.

    Those indexes carry no TTL and the prune pass only walks LIVE routes, so a delete that
    left them behind would strand them unreachable forever; the answer records they name
    keep their own retention TTL and are not touched.

    The reclamation is RETRYABLE, because it can be interrupted (a socket timeout, a
    SIGTERM, a Redis blip) after the routing row is already gone: a name whose route index
    survives is one whose reclamation is still owed, so this door re-runs it and answers
    ``removed=false`` rather than the 404 that would leave those keys unnameable forever.
    Only a name that neither routes nor owes reclamation is the loud 404. A name that is not
    a valid slug is a 400. Returns ``{"removed", "route_name"}``, where ``removed`` says
    whether THIS call removed the routing row.
    """
    _validate_route_name(route_name)
    manager = _require_backend()
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    store = ConversationRecordStore(ConversationsSettings())
    removed = await manager.delete_route(route_name)
    if not removed and await store.count_route_threads(route_name) == 0:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    # After the routing row, never before: no further message can open a thread on a name
    # that no longer routes. A turn already IN FLIGHT still completes behind this, which is
    # why the create writes the thread indexes only while the row stands and the completion
    # write re-stamps the route index only while the thread's own index still holds
    # members — either one unguarded would re-create a pair nothing walks and no TTL expires.
    #
    # Cancel every async ``ask_user`` parked on each of the route's threads BEFORE the
    # indexes go, so deleting the route does not orphan a park (its expiry reaper would
    # later fire a continuation into a thread whose route is gone, and its channel
    # correlation would stay muted until the deadline). Enumerated up front from the route
    # thread index; idempotent, so this door's own retry re-runs it cleanly.
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    for thread_id in await store.route_thread_ids(route_name):
        await cancel_parks_for_thread(thread_id)
    await store.drop_route_threads(route_name)
    return {"removed": removed, "route_name": route_name}
