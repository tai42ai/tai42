"""``MEDIA_INGEST_*`` config for the served-media ingestion chokepoint and its reaper.

MIME allowlists are SETTINGS, not wire constants: ingested bytes never ride the wire (they are
fetched/uploaded, then served by reference), so their bounds depend on the deployment's storage
quota. The outbound ``MEDIA_*`` constants (:mod:`tai42_contract.interactions.models.media`) bound
wire payloads and stay constants. The per-kind byte caps are a separate ``MEDIA_INGEST_*`` group
(:class:`tai42_kit.interactions.MediaIngestCapSettings`) the ingest seam reads alongside these.

A ``@model_validator`` proves at boot that every allowlisted image/audio/video type is
identifiable by one of the seam's two matchers (``filetype`` 1.2.0 or the in-seam ISOBMFF brand
matcher), so a misconfigured allowlist fails the process LOUDLY rather than silently rejecting
every upload of that type at runtime.
"""

from __future__ import annotations

import filetype
from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.interactions.models import MediaKind
from tai42_kit.settings import TaiBaseSettings, settings_cache


class MediaIngestSettings(TaiBaseSettings):
    """``MEDIA_INGEST_*`` settings: the MIME allowlists, the pending TTL, the reaper cadence."""

    model_config = SettingsConfigDict(env_prefix="MEDIA_INGEST_")

    # Per-kind MIME allowlists. Active/renderable content (svg/html/xhtml/xml/script) is ABSENT
    # from every default and must never be added — it is never served inline. A deployment
    # overrides a list wholesale via the ``MEDIA_INGEST_<KIND>_MIME_ALLOWLIST`` env JSON.
    image_mime_allowlist: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
    audio_mime_allowlist: frozenset[str] = frozenset({"audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr"})
    video_mime_allowlist: frozenset[str] = frozenset({"video/mp4", "video/3gpp", "video/quicktime"})
    document_mime_allowlist: frozenset[str] = frozenset(
        {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
            "text/csv",
        }
    )

    # The ONLY document types accepted on a ``filetype``-``None`` sniff (text-like bodies for which
    # ``filetype`` has no matcher); every other document type requires a positive sniff, and
    # image/audio/video always require one. Must be a subset of the document allowlist.
    document_none_sniff_mime_allowlist: frozenset[str] = frozenset({"text/plain", "text/csv"})

    # Seconds between media-retention reaper passes. Must be positive.
    reaper_interval_seconds: int = Field(default=300, gt=0)

    # Seconds a two-phase upload (bytes ingested with no ``origin.message_id``) is retained
    # before ``bind_media`` promotes it. Above the platform's compose-then-send timings, below
    # the interactions idle horizon so an abandoned upload's blob is short-lived. Must be positive.
    pending_ttl_seconds: int = Field(default=900, gt=0)

    def allowlist_for(self, kind: MediaKind) -> frozenset[str]:
        """The per-kind MIME allowlist for a sniffed ``kind`` (image/audio/video/document)."""
        lists = {
            MediaKind.IMAGE: self.image_mime_allowlist,
            MediaKind.AUDIO: self.audio_mime_allowlist,
            MediaKind.VIDEO: self.video_mime_allowlist,
            MediaKind.DOCUMENT: self.document_mime_allowlist,
        }
        if kind not in lists:
            raise ValueError(
                f"no ingest allowlist for media kind {kind.value!r} — only image/audio/video/document are ingested"
            )
        return lists[kind]

    @model_validator(mode="after")
    def _document_none_sniff_is_a_subset(self) -> MediaIngestSettings:
        """Refuse a None-sniff document type that is not itself on the document allowlist."""
        stray = self.document_none_sniff_mime_allowlist - self.document_mime_allowlist
        if stray:
            raise ValueError(
                f"MEDIA_INGEST_DOCUMENT_NONE_SNIFF_MIME_ALLOWLIST entries must be on the document allowlist, "
                f"got off-list {sorted(stray)}"
            )
        return self

    @model_validator(mode="after")
    def _validate_binary_types_sniffable(self) -> MediaIngestSettings:
        """Every allowlisted image/audio/video type must be identifiable by one of the two matchers.

        A type in neither ``filetype`` 1.2.0's registry nor the in-seam ISOBMFF brand table would be
        silently rejected on every upload (the positive-sniff requirement can never be met); this
        raises at boot instead of failing silently at runtime.
        """
        from tai42_skeleton.interactions.media_ingest import ISOBMFF_BRAND_MIMES

        filetype_mimes = {t.mime for t in filetype.TYPES}
        identifiable = filetype_mimes | ISOBMFF_BRAND_MIMES
        for kind, allowlist in (
            ("image", self.image_mime_allowlist),
            ("audio", self.audio_mime_allowlist),
            ("video", self.video_mime_allowlist),
        ):
            for mime in allowlist:
                if mime not in identifiable:
                    raise ValueError(
                        f"MEDIA_INGEST allowlisted {kind} type {mime!r} is identifiable by neither "
                        "filetype 1.2.0 nor the ISOBMFF brand matcher"
                    )
        return self


@settings_cache
def media_ingest_settings() -> MediaIngestSettings:
    """The cached ``MediaIngestSettings`` for this process."""
    return MediaIngestSettings()
