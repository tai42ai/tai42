"""``MEDIA_INGEST_*`` settings: defaults, env overrides, and the boot sniffability validator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_skeleton.settings.media_ingest import MediaIngestSettings, media_ingest_settings


@pytest.fixture(autouse=True)
def _clear_cache():
    media_ingest_settings.cache_clear()
    yield
    media_ingest_settings.cache_clear()


def test_media_ingest_settings_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_INGEST_PENDING_TTL_SECONDS", raising=False)
    media_ingest_settings.cache_clear()
    settings = media_ingest_settings()

    assert settings.pending_ttl_seconds == 900
    assert settings.reaper_interval_seconds == 300

    # Active/renderable content is absent from every allowlist default.
    active = {"image/svg+xml", "text/html", "application/xhtml+xml", "text/javascript", "application/xml"}
    for allowlist in (
        settings.image_mime_allowlist,
        settings.audio_mime_allowlist,
        settings.video_mime_allowlist,
        settings.document_mime_allowlist,
    ):
        assert not (allowlist & active)

    # video/3gpp passes the boot validator on its membership in filetype.TYPES (its genuine
    # runtime detection is the ISOBMFF matcher, since filetype's M3gp misses the offset).
    assert "video/3gpp" in settings.video_mime_allowlist

    # Env overrides apply.
    monkeypatch.setenv("MEDIA_INGEST_PENDING_TTL_SECONDS", "60")
    media_ingest_settings.cache_clear()
    overridden = media_ingest_settings()
    assert overridden.pending_ttl_seconds == 60


def test_media_ingest_settings_rejects_unidentifiable_binary_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_INGEST_VIDEO_MIME_ALLOWLIST", '["video/mp4", "video/x-nonexistent"]')
    media_ingest_settings.cache_clear()
    with pytest.raises(ValidationError, match="video/x-nonexistent"):
        media_ingest_settings()


def test_document_none_sniff_must_be_a_subset() -> None:
    with pytest.raises(ValidationError, match="document allowlist"):
        MediaIngestSettings(document_none_sniff_mime_allowlist=frozenset({"text/made-up"}))
