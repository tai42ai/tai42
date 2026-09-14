"""The shared surface every record-store concern mixin is typed against — the settings a
mixin reads and the key-layout/codec primitives one concern implements and the others call
on the composed store."""

from __future__ import annotations

from abc import ABC, abstractmethod

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings


class RecordStoreBase(ABC):
    """The persistence contract every concern mixin shares: the conversations settings and
    the three key-layout/codec primitives implemented once (by the write concern) and reached
    through ``self`` by the delivery, index and query concerns on the composed store."""

    settings: ConversationsSettings

    @abstractmethod
    def _record_keys(
        self,
        message_id: str,
        target: DeliveryStatus | None = None,
        thread: ConversationRecord | None = None,
        *,
        route_row: bool = False,
    ) -> list[str]:
        """``[record key, every status index, the target status index, the record's two
        thread indexes, the routing row]`` for a record-mutating script."""

    @abstractmethod
    def _index_score(self, status: DeliveryStatus, now: float) -> str:
        """The index member's expiry score for ``status`` at ``now``."""

    @abstractmethod
    def _from_hash(self, hashed: dict[str, str]) -> ConversationRecord:
        """The record a stored hash decodes to."""
