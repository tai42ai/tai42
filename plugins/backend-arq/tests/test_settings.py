"""``ArqSettings`` connection identity and its ``TAI_DEFAULT_*`` fallback.

``redis_url`` / ``redis_max_connections`` fall back to the shared
``TAI_DEFAULT_*`` namespace when ``ARQ_*`` leaves them unset; a set ``ARQ_``
value always wins.
"""

from __future__ import annotations

import os

import pytest

from tai42_backend_arq.settings import ArqSettings

# Env prefixes any test here may touch — stripped before each test so ambient
# env (or a leaked var) can never colour a resolution.
_TEST_ENV_PREFIXES = ("ARQ_", "TAI_DEFAULT_")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every relevant env var so each test controls the whole set."""
    for key in list(os.environ):
        if key.startswith(_TEST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


def test_nothing_set_keeps_class_defaults() -> None:
    settings = ArqSettings()
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.redis_max_connections is None


def test_redis_url_falls_back_to_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert ArqSettings().redis_url == "redis://shared:6379/0"


def test_specific_redis_url_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_REDIS_URL", "redis://arq:6380/1")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert ArqSettings().redis_url == "redis://arq:6380/1"


def test_redis_max_connections_falls_back_to_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    assert ArqSettings().redis_max_connections == 42


def test_specific_redis_max_connections_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_REDIS_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    assert ArqSettings().redis_max_connections == 7
