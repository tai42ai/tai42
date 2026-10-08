"""The conversation-route write service: the save rules every route write runs, the door's and the restore's.

The route's caller rules (the target's existence on the live registries, the caller's authority to
delegate the execution key) stay at the door that has a caller; this service judges the route
itself. Its refusals are typed so each caller classifies them by exact type:
:class:`RouteBindRefusedError` (the target cannot bind the route), :class:`RouteWriteRefusedError`
(an expression that does not compile, an ``our_identity`` that is not an address or is already
routed), and :class:`DoorFlipRefusedError` from the store's own write. Anything else is not a
refusal of the route and propagates.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass

from tai42_contract.app import tai42_app
from tai42_contract.conversations import ConversationRoute
from tai42_kit.utils.data.jq_util import compile_check

from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager
from tai42_skeleton.conversations.target_validators import active_target_candidate_body, target_bind_refusal_lines
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError

# The named jq variables each expression may read, declared at compilation so an author
# referencing one passes (jq resolves ``$name`` references at compile). The four door jqs read the
# run's parked interactions as ``$parked``; ``reply_expr`` additionally reads the turn ids/subject
# as ``$turn`` and the caller ask entries as ``$asks``.
_DOOR_EXPR_VARIABLES = ("parked",)
_REPLY_EXPR_VARIABLES = ("asks", "parked", "turn")


class RouteBindRefusedError(Exception):
    """A route whose target cannot bind it: the platform's and the target owner's refusal lines."""

    def __init__(self, route_name: str, lines: Sequence[str]) -> None:
        """Record the refused route and the refusal lines, joined into the message."""
        self.route_name = route_name
        self.lines = tuple(lines)
        super().__init__("; ".join(self.lines))


class RouteWriteRefusedError(ValueError):
    """A route the write refuses for its own content: an expression, its ``our_identity``, or its claim."""


@dataclass(frozen=True)
class RouteWriteResult:
    """The outcome of a route write: whether the row was new, the row as stored, and its once-shown secret."""

    created: bool
    route: ConversationRoute
    callback_secret: str | None


async def _assert_bindable(route: ConversationRoute) -> None:
    """Run the route bind check against the target's active body, refusing a route its target cannot bind.

    The platform's own rules (an asking agent, reached as either kind, needs a reply/resume path)
    plus the one validator the target's owner registered. A target the live registry does not know
    resolves to itself, so a route whose target is registered later is judged by those rules alone.
    """
    candidate = await active_target_candidate_body(route.target_kind, route.target_name)
    lines = await target_bind_refusal_lines(route, candidate)
    if lines:
        raise RouteBindRefusedError(route.route_name, lines)


async def _assert_exprs_compile(route: ConversationRoute) -> None:
    """Render each templated jq program and compile it, declaring the variables it may read.

    A by-id text whose stored resource cannot be fetched is refused, naming the field and the id.
    Both target kinds may carry the door jqs.
    """
    exprs = (
        ("start_expr", route.start_expr, _DOOR_EXPR_VARIABLES),
        ("cancel_expr", route.cancel_expr, _DOOR_EXPR_VARIABLES),
        ("resume_expr", route.resume_expr, _DOOR_EXPR_VARIABLES),
        ("extras_expr", route.extras_expr, _DOOR_EXPR_VARIABLES),
        ("reply_expr", route.reply_expr, _REPLY_EXPR_VARIABLES),
    )
    for field, text, variables in exprs:
        if text is None:
            continue
        try:
            program = await tai42_app.storage.resource_manager.render_templated_text(text)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise RouteWriteRefusedError(
                f"{field} references stored id {text.id!r}, which could not be fetched: {exc}"
            ) from exc
        try:
            compile_check(program, variables=variables)
        except Exception as exc:
            raise RouteWriteRefusedError(f"invalid {field}: {exc}") from exc


async def _unclaimed_channel_identity(
    manager: BaseConversationsManager, *, route_name: str, channel: str, our_identity: str
) -> str:
    """The canonical ``our_identity`` a ``channel`` row is STORED under.

    Refused when another route already claims that ``(channel, identity)`` pair. Inbound routing
    matches the canonical form, so a second claimant would make every message to that identity
    unresolvable; it is refused here at the write instead.
    """
    try:
        identity = canonical_address(our_identity)
    except ValueError as exc:
        raise RouteWriteRefusedError(f"invalid our_identity: {exc}") from exc
    routes, _ = await manager.list_routes()
    for row in routes.values():
        if (
            row.route_name != route_name
            and row.door == "channel"
            and row.channel == channel
            and row.our_identity is not None
            and canonical_address(row.our_identity) == identity
        ):
            raise RouteWriteRefusedError(
                f"channel {channel!r} identity {identity!r} is already routed by {row.route_name!r}"
            )
    return identity


async def write_route(route: ConversationRoute, *, manager: BaseConversationsManager) -> RouteWriteResult:
    """Write ``route`` (an upsert) after its save rules, minting its callback secret.

    In order: the bind check, the expressions, the canonical ``our_identity`` and its claim, then
    the store write. An ``api`` row that declares a ``callback_url`` gets a fresh
    ``callback_secret`` (returned once); every other row carries none. The store write refuses a
    door flip on a route that holds threads with :class:`DoorFlipRefusedError`, writing nothing.
    """
    await _assert_bindable(route)
    await _assert_exprs_compile(route)
    update: dict[str, object] = {}
    # Only a ``channel`` row carries both fields; its identity is stored canonicalized.
    if route.channel is not None and route.our_identity is not None:
        update["our_identity"] = await _unclaimed_channel_identity(
            manager, route_name=route.route_name, channel=route.channel, our_identity=route.our_identity
        )
    # Signs the api-door callback; a ``channel`` row and a poll-only api row (no callback
    # declared) sign nothing and carry no secret.
    callback_secret = secrets.token_urlsafe(32) if route.door == "api" and route.callback_url is not None else None
    update["callback_secret"] = callback_secret
    stored = route.model_copy(update=update)
    created = await manager.put_route(stored)
    return RouteWriteResult(created=created, route=stored, callback_secret=callback_secret)


__all__ = ["RouteBindRefusedError", "RouteWriteRefusedError", "RouteWriteResult", "write_route"]
