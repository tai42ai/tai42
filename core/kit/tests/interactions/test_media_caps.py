"""``MEDIA_INGEST_*`` per-kind byte caps: defaults, ``gt=0``, ``cap_for`` per kind, ``max_cap``, env override."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tai42_contract.interactions.models import MediaKind

from tai42_kit.interactions import MediaIngestCapSettings, media_ingest_cap_settings


@pytest.fixture(autouse=True)
def _clear_cache():
    media_ingest_cap_settings.cache_clear()
    yield
    media_ingest_cap_settings.cache_clear()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MEDIA_INGEST_MAX_IMAGE_BYTES",
        "MEDIA_INGEST_MAX_AUDIO_BYTES",
        "MEDIA_INGEST_MAX_VIDEO_BYTES",
        "MEDIA_INGEST_MAX_DOCUMENT_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    media_ingest_cap_settings.cache_clear()
    settings = media_ingest_cap_settings()

    assert settings.max_image_bytes == 8 * 1024 * 1024
    assert settings.max_audio_bytes == 16 * 1024 * 1024
    assert settings.max_video_bytes == 25 * 1024 * 1024
    assert settings.max_document_bytes == 25 * 1024 * 1024


def test_cap_for_each_kind() -> None:
    settings = MediaIngestCapSettings()
    assert settings.cap_for(MediaKind.IMAGE) == settings.max_image_bytes
    assert settings.cap_for(MediaKind.AUDIO) == settings.max_audio_bytes
    assert settings.cap_for(MediaKind.VIDEO) == settings.max_video_bytes
    assert settings.cap_for(MediaKind.DOCUMENT) == settings.max_document_bytes


def test_max_cap_is_the_largest() -> None:
    settings = MediaIngestCapSettings()
    assert settings.max_cap == max(
        settings.max_image_bytes,
        settings.max_audio_bytes,
        settings.max_video_bytes,
        settings.max_document_bytes,
    )
    assert settings.max_cap == 25 * 1024 * 1024


@pytest.mark.parametrize(
    "name",
    [
        "max_image_bytes",
        "max_audio_bytes",
        "max_video_bytes",
        "max_document_bytes",
    ],
)
def test_caps_must_be_positive(name: str) -> None:
    with pytest.raises(ValidationError):
        MediaIngestCapSettings(**{name: 0})


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_INGEST_MAX_IMAGE_BYTES", "1234")
    media_ingest_cap_settings.cache_clear()
    settings = media_ingest_cap_settings()
    assert settings.max_image_bytes == 1234
    assert settings.cap_for(MediaKind.IMAGE) == 1234
