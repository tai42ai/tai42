"""The served-media ingestion facet (``app.media``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from tai42_contract.conversations import InboundMediaKind
from tai42_contract.interactions.models.media import IngestedMedia, MediaOrigin


@runtime_checkable
class AppMedia(Protocol):
    """The served-media ingestion namespace (``app.media``).

    A channel adapter or the web upload door streams inbound bytes through the ONE
    ``ingest_media`` chokepoint; the platform caps, sniffs, stores, and mints a served
    reference, returning the typed :class:`IngestedMedia`. The body is the skeleton's; this facet
    is the contract the doors and the impl agree on.
    """

    async def ingest_media(
        self,
        *,
        source: AsyncIterator[bytes],
        kind_hint: InboundMediaKind | str | None,
        declared_mime: str | None,
        filename: str | None,
        declared_size: int | None,
        integrity_sha256: str | None,
        origin: MediaOrigin,
    ) -> IngestedMedia:
        """Ingest one streamed inbound media, returning its served reference and metadata.

        Reads ``source`` under the platform's per-kind byte cap (raising loudly on a cap crossing,
        never truncating), sniffs the real type, agrees it against ``declared_mime``, stores the
        bytes by reference, and returns an :class:`IngestedMedia` whose ``item`` carries the served
        ``{MEDIA_ROUTE_PREFIX}{media_id}`` url. Raises a
        :class:`~tai42_contract.interactions.MediaIngestError` subclass on an over-cap /
        disallowed-type / unfetchable / store-unavailable outcome — never a silent skip.

        Two-phase (upload-then-attach) support rides ``origin.message_id``: when it is ``None`` the
        item is ingested PENDING (retained only for ``MEDIA_INGEST_PENDING_TTL_SECONDS``, owned by
        ``(origin.channel_id, origin.participant_identity)``) so a door that receives the bytes
        before the owning message exists (the web upload door) can bind it later with
        :meth:`bind_media`; when it is set the item is bound at ingest to the record's retention
        horizon (the vendor channels, which know the message id at fetch time).
        ``IngestedMedia.pending`` reports which state resulted.
        """
        ...

    async def bind_media(self, media_id: str, *, origin: MediaOrigin) -> IngestedMedia:
        """Promote a PENDING ingested media to the owning conversation record's retention horizon.

        Called when a message finally references a ``media_id`` ingested with no ``origin.message_id``
        (``origin.message_id`` is REQUIRED here). Idempotent when the item is already bound to the
        SAME message. Raises loudly, never silently:
        :class:`~tai42_contract.interactions.MediaNotFoundError` — one uniform error for an unknown,
        an expired-pending, or a ``media_id`` NOT owned by the caller's
        ``(origin.channel_id, origin.participant_identity)`` (no ownership oracle — the three cases
        are indistinguishable to the caller);
        :class:`~tai42_contract.interactions.MediaAlreadyBoundError` — the item is already bound to a
        DIFFERENT message. Returns the now-bound :class:`IngestedMedia` (``pending == False``).
        """
        ...


__all__ = ["AppMedia"]
