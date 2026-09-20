"""Resolve a route, its multichannel context and the thread key for an inbound.

The per-accept multichannel context decides the thread key and, later, the pairing turn
and greeting; the channel-door route lookup matches ``(channel, our_identity)`` by exact
canonical equality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tai42_contract.conversations import ConversationDoor, ConversationRoute, PersonAddress

from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.pair_codes import MintingConversation
from tai42_skeleton.conversations.persons import PairingTarget
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.errors import ConversationRouteResolutionError
from tai42_skeleton.conversations.turn.keys import _person_thread_id, _thread_id, _throttle_source_key


@dataclass(frozen=True)
class _Multichannel:
    """The per-accept multichannel context for a target with ``multichannel: true``.

    Carries the target, the sending address in the door's own terms, the accountable
    party the redeem throttle keys on, and the first-contact greeting template. ``None``
    everywhere multichannel is OFF, which leaves an unconfigured or unlinked
    conversation unchanged.

    ``address`` is the PERSON IDENTITY (the thread, the transcript, the pair-code's
    stored conversation); ``accountable`` is the rotation-resistant party the
    brute-force throttle scopes to — the api ``caller_principal`` or the channel
    ``cap_key`` — and is NEVER the conversation address.
    """

    target: PairingTarget
    door: ConversationDoor
    channel: str | None
    our_identity: str | None
    address: str
    accountable: str
    route_name: str
    greeting_template: str | None

    def address_row(self) -> PersonAddress:
        """This sending address as a fresh :class:`PersonAddress` row (its route attributed)."""
        return PersonAddress(
            door=self.door,
            routes=[self.route_name],
            channel=self.channel,
            our_identity=self.our_identity,
            address=self.address,
            linked_at=datetime.now(UTC),
        )

    def minting_conversation(self) -> MintingConversation:
        """This conversation as the value a minted pair code stores.

        The redeem side rebuilds a complete address from it.
        """
        return MintingConversation(
            target_kind=self.target.target_kind,
            target_name=self.target.target_name,
            route_name=self.route_name,
            door=self.door,
            channel=self.channel,
            our_identity=self.our_identity,
            address=self.address,
        )

    def throttle_source_key(self) -> str:
        """The redeem-throttle source key for this accept.

        This accept's DOOR-QUALIFIED accountable party (the api ``caller_principal`` or
        the channel ``cap_key``), NOT the conversation address — so an attacker cannot
        rotate a caller-composed address to dodge the lock.
        """
        return _throttle_source_key(self.door, self.accountable)


async def _multichannel_context(
    route: ConversationRoute,
    *,
    door: ConversationDoor,
    channel: str | None,
    our_identity: str | None,
    address: str,
    accountable: str,
) -> _Multichannel | None:
    """The multichannel context for ``route``, or ``None`` when multichannel is off.

    ``None`` when the target has no config row or its ``multichannel`` is off
    (default-false). Read once per accept, BEFORE the gates: it decides the thread key
    and, later, the pairing turn and greeting.

    ``address`` is the conversation identity; ``accountable`` is the rotation-resistant
    party the redeem throttle scopes to (the caller of the door, the same key its rate
    cap uses).
    """
    config = await accessors._config_store().get(route.target_kind, route.target_name)
    if config is None or not config.multichannel:
        return None
    return _Multichannel(
        target=PairingTarget(target_kind=route.target_kind, target_name=route.target_name),
        door=door,
        channel=channel,
        our_identity=our_identity,
        address=address,
        accountable=accountable,
        route_name=route.route_name,
        greeting_template=config.greeting_template,
    )


async def _resolve_thread_id(route: ConversationRoute, multichannel: _Multichannel | None, address: str) -> str:
    """The thread key for this accept.

    A LINKED person on any multichannel target keys the aggregated
    ``bridge:@person:{person_id}`` thread; everyone else uses the route-keyed
    ``bridge:{route}:{address}``. Read-only: no person row is created here, so a
    redelivered, refused or shed message never mints identity.

    In-flight merge race: a turn admitted under the old key while the merge lands
    completes under that key (its FIFO slot lives there); the NEXT message keys to the
    person thread. Histories are never migrated (linked memory starts at the pairing
    moment).
    """
    if multichannel is not None:
        person = await accessors._person_store().get_person(
            multichannel.target,
            door=multichannel.door,
            channel=multichannel.channel,
            our_identity=multichannel.our_identity,
            address=multichannel.address,
        )
        if person is not None and len(person.addresses) > 1:
            return _person_thread_id(person.person_id)
    return _thread_id(route.route_name, address)


async def _resolve_channel_route(channel: str, our_identity_canonical: str) -> ConversationRoute:
    """The single ``door=channel`` route matching ``(channel, our_identity)`` exactly.

    Matched by EXACT equality on the canonical address form. No match raises
    :class:`ConversationRouteResolutionError`; more than one is a corrupt table and
    raises rather than picking one.
    """
    routes = await cache.get_conversations_manager().list_routes()
    matches = [
        route
        for route in routes.values()
        if route.door == "channel"
        and route.channel == channel
        and route.our_identity is not None
        and canonical_address(route.our_identity) == our_identity_canonical
    ]
    if not matches:
        raise ConversationRouteResolutionError(
            f"no channel route bound to channel {channel!r} identity {our_identity_canonical!r}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"conversations: {len(matches)} channel routes claim channel {channel!r} identity "
            f"{our_identity_canonical!r}; the routing table is inconsistent"
        )
    return matches[0]
