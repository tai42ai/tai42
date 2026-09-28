"""The platform's per-kind media byte caps — shared by the ingest seam and every upload-shaped door.

Byte caps are SETTINGS, not wire constants: ingested bytes never ride the wire (they are
fetched/uploaded, then served by reference), so their bounds depend on the deployment's storage
quota. A body over its sniffed kind's cap is rejected loudly mid-stream, never truncated.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.interactions.models import MediaKind

from tai42_kit.settings import TaiBaseSettings, settings_cache


class MediaIngestCapSettings(TaiBaseSettings):
    """``MEDIA_INGEST_*`` per-kind byte caps for served-media ingestion."""

    model_config = SettingsConfigDict(env_prefix="MEDIA_INGEST_")

    # Per-kind byte caps. Video and document take the largest bound; image and audio take their own
    # tighter bounds, well above typical payloads. Must be positive.
    max_image_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_audio_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    max_video_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_document_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    def cap_for(self, kind: MediaKind) -> int:
        """The per-kind byte cap for a sniffed ``kind`` (image/audio/video/document)."""
        caps = {
            MediaKind.IMAGE: self.max_image_bytes,
            MediaKind.AUDIO: self.max_audio_bytes,
            MediaKind.VIDEO: self.max_video_bytes,
            MediaKind.DOCUMENT: self.max_document_bytes,
        }
        if kind not in caps:
            raise ValueError(
                f"no ingest cap for media kind {kind.value!r} — only image/audio/video/document are ingested"
            )
        return caps[kind]

    @property
    def max_cap(self) -> int:
        """The largest per-kind cap — the pre-read worst case when the kind is not yet known."""
        return max(self.max_image_bytes, self.max_audio_bytes, self.max_video_bytes, self.max_document_bytes)


@settings_cache
def media_ingest_cap_settings() -> MediaIngestCapSettings:
    """The cached :class:`MediaIngestCapSettings` for this process."""
    return MediaIngestCapSettings()
