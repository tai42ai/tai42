"""The plugin owns its configuration: the key namespace and the Redis connection.

The contract injects neither, so these live in the plugin's ``TAI_IDENTITY_*`` env.
"""

from __future__ import annotations

from tai42_identity_redis.settings import RedisIdentitySettings


def test_key_prefix_default_and_override(monkeypatch):
    # The identity-record key namespace defaults here, in the plugin's own settings.
    assert RedisIdentitySettings().key_prefix == "ac:key:"
    monkeypatch.setenv("TAI_IDENTITY_KEY_PREFIX", "tenant-x:key:")
    assert RedisIdentitySettings().key_prefix == "tenant-x:key:"


def test_redis_url_reads_own_env(monkeypatch):
    monkeypatch.setenv("TAI_IDENTITY_REDIS_URL", "redis://identity-host:6379/3")
    assert RedisIdentitySettings().redis.redis_url == "redis://identity-host:6379/3"


def test_redis_url_falls_back_to_the_shared_default(monkeypatch):
    # Unset, the connection rides the kit-wide default so a single-Redis deployment
    # configures one URL.
    monkeypatch.delenv("TAI_IDENTITY_REDIS_URL", raising=False)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    assert RedisIdentitySettings().redis.redis_url == "redis://shared:6379/0"


def test_connection_tunes_the_auth_path_resilience_defaults():
    redis = RedisIdentitySettings().redis
    assert redis.socket_timeout == 5
    assert redis.socket_connect_timeout == 5
    assert redis.retry_on_timeout is True
