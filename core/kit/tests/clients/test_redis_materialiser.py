"""The kit's one Redis materialiser.

``redis_from_url_kwargs`` turns the kit's client kwargs into ``Redis.from_url`` kwargs and
``redis_connection_options`` builds the behaviour half (the retry and the timeouts) on its
own. The retry they attach is the kit's logged retry, named by a setting, never the URL.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("redis")

import redis
from redis.backoff import ExponentialBackoff, NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tai42_kit.clients.backoff import CONNECT_RETRY_BACKOFF_BASE_SECONDS, CONNECT_RETRY_BACKOFF_CAP_SECONDS
from tai42_kit.clients.impl import redis as redis_mod
from tai42_kit.clients.impl import redis_connections as connections_mod

from ._redis_log import MARKER_PASSWORD


def test_the_constants_are_redis_py_default_backoff() -> None:
    assert CONNECT_RETRY_BACKOFF_BASE_SECONDS == 0.008
    assert CONNECT_RETRY_BACKOFF_CAP_SECONDS == 0.512


@pytest.mark.parametrize(
    ("sync", "retry_cls"),
    [(False, "_LoggedAsyncRetry"), (True, "_LoggedSyncRetry")],
    ids=["async", "sync"],
)
def test_retry_attempts_attach_the_kit_logged_retry_with_the_kit_backoff(sync: bool, retry_cls: str) -> None:
    kwargs = {"url": "redis://h:6379/0", "decode_responses": True, "env_prefix": "X_", "retry_attempts": 2}
    before = dict(kwargs)
    out = redis_mod.redis_from_url_kwargs(kwargs, sync=sync)
    assert kwargs == before
    retry = out.pop("retry")
    assert type(retry) is getattr(redis_mod, retry_cls)
    assert retry._retries == 2
    assert isinstance(retry._backoff, ExponentialBackoff)
    assert retry._backoff._base == CONNECT_RETRY_BACKOFF_BASE_SECONDS
    assert retry._backoff._cap == CONNECT_RETRY_BACKOFF_CAP_SECONDS
    assert retry._label == "X_REDIS_URL"
    if sync:
        assert out.pop("connection_class") is connections_mod.kit_sync_connection_class(redis.Connection)
    assert out == {
        "url": "redis://h:6379/0",
        "decode_responses": True,
        "retry_on_error": [RedisConnectionError, RedisTimeoutError, OSError],
    }


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_retry_on_timeout_alone_attaches_one_logged_retry_with_no_backoff(sync: bool) -> None:
    out = redis_mod.redis_from_url_kwargs({"url": "redis://h", "retry_on_timeout": True}, sync=sync)
    retry = out["retry"]
    assert isinstance(retry, redis_mod._LoggedSyncRetry if sync else redis_mod._LoggedAsyncRetry)
    assert retry._retries == 1
    assert isinstance(retry._backoff, NoBackoff)
    assert out["retry_on_timeout"] is True
    assert "retry_on_error" not in out


def test_no_retry_option_attaches_no_retry() -> None:
    out = redis_mod.redis_from_url_kwargs({"url": "redis://h", "socket_timeout": 2, "retry_attempts": 0})
    assert out == {"url": "redis://h", "socket_timeout": 2}


def test_the_materialiser_validates_the_url_and_the_keys() -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        redis_mod.redis_from_url_kwargs({"url": None, "env_prefix": "X_"})
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        redis_mod.redis_from_url_kwargs({"url": "redis://h", "bogus": 1})


@pytest.mark.parametrize(
    ("kwargs", "label", "expected"),
    [
        ({"url": "redis://h:1/2", "env_prefix": ""}, None, "REDIS_URL"),
        ({"url": "redis://h:1/2", "env_prefix": "ARQ_"}, None, "ARQ_REDIS_URL"),
        ({"url": "redis://h:1/2", "env_prefix": "ARQ_"}, "MY_LABEL", "MY_LABEL"),
        ({"url": f"redis://user:{MARKER_PASSWORD}@h:1/2"}, None, "h:1/2"),
        ({"url": f"rediss://:{MARKER_PASSWORD}@h/3"}, None, "h/3"),
        ({"url": "unix:///run/redis.sock"}, None, "/run/redis.sock"),
    ],
    ids=["base-prefix", "prefix", "explicit", "url", "url-no-port", "unix"],
)
def test_the_label_is_a_setting_name_or_the_url_without_credentials(
    kwargs: dict[str, Any], label: str | None, expected: str
) -> None:
    out = redis_mod.redis_from_url_kwargs({**kwargs, "retry_attempts": 1}, label=label)
    assert out["retry"]._label == expected


def test_the_behaviour_half_needs_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    out = redis_mod.redis_connection_options({"retry_attempts": 2, "socket_timeout": 3}, sync=True, label="L")
    retry = out.pop("retry")
    assert type(retry) is redis_mod._LoggedSyncRetry
    assert (retry._retries, retry._label) == (2, "L")
    assert out == {"socket_timeout": 3, "retry_on_error": [RedisConnectionError, RedisTimeoutError, OSError]}


@pytest.mark.parametrize("key", ["url", "max_connections", "bogus"])
def test_the_behaviour_half_refuses_any_other_key(key: str) -> None:
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        redis_mod.redis_connection_options({key: 1}, label="L")
