"""``ArqSettings.redis``: the kit Redis connection settings under the ``ARQ_`` prefix.

``redis_url`` / ``redis_max_connections`` fall back to the shared
``TAI_DEFAULT_*`` namespace when ``ARQ_*`` leaves them unset; a set ``ARQ_``
value always wins. The retry and the socket timeouts are the kit's, read from
``ARQ_*`` alone.
"""

from __future__ import annotations

import os

import pytest
from tai42_kit.settings import registered_settings

from tai42_backend_arq.settings import ArqRedisSettings, ArqSettings

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
    settings = ArqSettings().redis
    assert isinstance(settings, ArqRedisSettings)
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.redis_max_connections is None
    # arq's own connect timeout, kept as the default.
    assert settings.socket_connect_timeout == 1
    assert settings.retry_attempts == 0


def test_the_retry_and_timeouts_load_from_the_arq_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("ARQ_SOCKET_TIMEOUT", "3")
    monkeypatch.setenv("ARQ_SOCKET_CONNECT_TIMEOUT", "4")
    monkeypatch.setenv("ARQ_RETRY_ON_TIMEOUT", "true")

    settings = ArqSettings().redis
    assert settings.retry_attempts == 2
    assert settings.socket_timeout == 3
    assert settings.socket_connect_timeout == 4
    assert settings.retry_on_timeout is True


def test_redis_url_falls_back_to_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert ArqSettings().redis.redis_url == "redis://shared:6379/0"


def test_specific_redis_url_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_REDIS_URL", "redis://arq:6380/1")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert ArqSettings().redis.redis_url == "redis://arq:6380/1"


def test_redis_max_connections_falls_back_to_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    assert ArqSettings().redis.redis_max_connections == 42


def test_specific_redis_max_connections_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_REDIS_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    assert ArqSettings().redis.redis_max_connections == 7


def test_queue_name_defaults_to_the_arq_default() -> None:
    # arq's own default queue key, so an unconfigured deployment is unchanged.
    from arq.constants import default_queue_name

    assert ArqSettings().queue_name == default_queue_name


def test_queue_name_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_QUEUE_NAME", "tai42_e2e_abc123:arq:queue")

    assert ArqSettings().queue_name == "tai42_e2e_abc123:arq:queue"


def test_the_redis_connection_publishes_exactly_its_connection_settings() -> None:
    env_vars = {
        field.env_var
        for field in next(info for info in registered_settings() if info.name == "ArqRedisSettings").fields
    }
    assert env_vars == {
        "ARQ_REDIS_URL",
        "ARQ_REDIS_MAX_CONNECTIONS",
        "ARQ_RETRY_ATTEMPTS",
        "ARQ_RETRY_ON_TIMEOUT",
        "ARQ_SOCKET_TIMEOUT",
        "ARQ_SOCKET_CONNECT_TIMEOUT",
    }
