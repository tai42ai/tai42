"""The ``app.media`` namespace, forwarding to the served-media seam (ingest, bind, read).

``ingest_media`` streams one inbound media through the platform's cap/sniff/allowlist/store
chokepoint in :mod:`tai42_skeleton.interactions.media_ingest` and returns its served reference;
``bind_media`` promotes a pending two-phase upload to the record's horizon; ``read_media`` reads a
stored media back by its id through :mod:`tai42_skeleton.interactions.served_media`, the reader
the served route uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tai42_contract.conversations import InboundMediaKind
    from tai42_contract.interactions.models import IngestedMedia, MediaOrigin, StoredMedia

    from tai42_skeleton.app.server import TaiMCP


class MediaFacet:
    """``app.media`` — the served-media seam's entry surface (``AppMedia``): ingest, bind, read."""

    __slots__ = ("_app",)

    def __init__(self, app: TaiMCP) -> None:
        """Bind the owning ``app``."""
        self._app = app

    async def ingest_media(
        self,
        *,
        source: AsyncIterator[bytes],
        kind_hint: InboundMediaKind | str | None,
        declared_mime: str | None,
        filename: str | None,
        declared_size: int | None,
        origin: MediaOrigin,
    ) -> IngestedMedia:
        """Ingest one streamed inbound media, returning its served reference and metadata."""
        return await self._app._media_ingest_media(
            source=source,
            kind_hint=kind_hint,
            declared_mime=declared_mime,
            filename=filename,
            declared_size=declared_size,
            origin=origin,
        )

    async def bind_media(self, media_id: str, *, origin: MediaOrigin) -> IngestedMedia:
        """Promote a PENDING ingested media to the owning conversation record's retention horizon."""
        return await self._app._media_bind_media(media_id, origin=origin)

    async def read_media(self, media_id: str, *, max_bytes: int) -> StoredMedia:
        """Read the stored media the served route serves for ``media_id``, within ``max_bytes``."""
        return await self._app._media_read_media(media_id, max_bytes=max_bytes)
