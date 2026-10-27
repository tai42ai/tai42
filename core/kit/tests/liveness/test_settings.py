"""``BackendLivenessSettings`` — the liveness file's path, refresh margin and probe grace."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tai42_kit.liveness import BackendLivenessSettings, backend_liveness_settings
from tai42_kit.settings import registered_settings


def test_defaults() -> None:
    settings = BackendLivenessSettings()

    assert settings.path == Path("/tmp/tai-backend-liveness")
    assert settings.interval == 5.0
    assert settings.grace == 25.0


def test_env_overrides_every_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_PATH", str(tmp_path / "liveness"))
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_INTERVAL", "1.5")
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_GRACE", "4")

    settings = backend_liveness_settings()

    assert settings.path == tmp_path / "liveness"
    assert settings.interval == 1.5
    assert settings.grace == 4.0


def test_a_zero_grace_is_accepted() -> None:
    assert BackendLivenessSettings(grace=0).grace == 0.0


def test_relative_path_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be an absolute path"):
        BackendLivenessSettings(path=Path("relative/liveness"))


def test_a_non_positive_interval_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        BackendLivenessSettings(interval=0)


def test_a_negative_grace_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        BackendLivenessSettings(grace=-1)


def test_registered_with_every_field_excluded_from_reload() -> None:
    group = next(info for info in registered_settings() if info.name == "BackendLivenessSettings")

    assert group.env_prefix == "TAI_BACKEND_LIVENESS_"
    assert {field.name: field.reload_class for field in group.fields} == {
        "path": "excluded",
        "interval": "excluded",
        "grace": "excluded",
    }
