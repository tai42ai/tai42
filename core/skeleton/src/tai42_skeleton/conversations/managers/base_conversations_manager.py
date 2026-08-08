from abc import ABC, abstractmethod

from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.conversations.settings import ConversationsSettings


class DoorFlipRefused(Exception):
    """Raised by :meth:`BaseConversationsManager.put_route` when the write would change the
    ``door`` of a route that already holds threads. Carries what the refusing step read, so
    the caller's operator-facing message names the state the write was actually refused
    against and not a count read a round trip earlier."""

    def __init__(self, route_name: str, from_door: str, to_door: str, held: int) -> None:
        super().__init__(f"conversation route {route_name!r} holds {held} thread(s) on its {from_door!r} door")
        self.route_name = route_name
        self.from_door = from_door
        self.to_door = to_door
        self.held = held


class BaseConversationsManager(ABC):
    """The routing-row store — keyspace 4 of the conversation bridge: the durable,
    backed-up mapping from an inbound door to the agent a turn runs and the execution key
    it runs AS. The in-memory backend refuses every operation with a loud 501.
    """

    def __init__(self, settings: ConversationsSettings) -> None:
        self.settings = settings

    @abstractmethod
    async def put_route(self, route: ConversationRoute) -> bool:
        """Store ``route`` (an upsert — create or replace), keeping the name index in
        lockstep. Return ``True`` when the row is newly created, ``False`` when it
        replaced an existing row of the same name.

        Raise :class:`DoorFlipRefused` — writing nothing — when the row already stored
        under that name carries a DIFFERENT ``door`` and the route's thread index is not
        empty. The stored door, the thread count and the write are ONE step: the two doors
        key their threads differently, so a flip that landed while a first message was
        opening a thread would lock that thread's owner out of a transcript they own, and
        the revert would then be refused too."""
        ...

    @abstractmethod
    async def get_route(self, route_name: str) -> ConversationRoute | None:
        """The stored row for ``route_name`` (its ``callback_secret`` included, for the
        delivery executor), or ``None`` when no such route exists."""
        ...

    @abstractmethod
    async def delete_route(self, route_name: str) -> bool:
        """Remove the row for ``route_name``, keeping the name index in lockstep. Return
        ``True`` when a row was removed, ``False`` when none existed."""
        ...

    @abstractmethod
    async def list_routes(self) -> dict[str, ConversationRoute]:
        """Every stored routing row keyed by route name (each ``callback_secret``
        included, for internal consumers)."""
        ...
