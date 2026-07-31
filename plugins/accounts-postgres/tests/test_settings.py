"""Settings derivation: the Redis namespace and the hash-concurrency default."""

from __future__ import annotations

from tai42_accounts_postgres import settings as settings_module
from tai42_accounts_postgres.settings import AccountsSettings


def test_redis_key_prefix_defaults_to_pg_db(monkeypatch):
    """Unset, the Redis namespace derives from the plugin's pg_db (never a literal)."""
    monkeypatch.setenv("TAI_ACCOUNTS_PG_PG_DB", "deployment_b")
    s = AccountsSettings()
    assert s.pg.pg_db == "deployment_b"
    assert s.redis_key_prefix == "deployment_b"
    assert s.key_prefix == "deployment_b"


def test_redis_key_prefix_explicit_is_respected(monkeypatch):
    monkeypatch.setenv("TAI_ACCOUNTS_PG_PG_DB", "shared")
    monkeypatch.setenv("TAI_ACCOUNTS_REDIS_KEY_PREFIX", "tenant-1")
    s = AccountsSettings()
    assert s.key_prefix == "tenant-1"


def test_hash_concurrency_is_cpu_multiple(monkeypatch):
    monkeypatch.setattr(settings_module.os, "cpu_count", lambda: 4)
    s = AccountsSettings()
    assert s.login_hash_concurrency == settings_module._HASH_CONCURRENCY_CPU_MULTIPLE * 4


def test_hash_concurrency_falls_to_floor_when_cpu_unknown(monkeypatch, caplog):
    monkeypatch.setattr(settings_module.os, "cpu_count", lambda: None)
    with caplog.at_level("WARNING"):
        s = AccountsSettings()
    assert s.login_hash_concurrency == settings_module._HASH_CONCURRENCY_CPU_MULTIPLE
    assert "cpu_count" in caplog.text


def test_defaults_are_the_provisional_values():
    s = AccountsSettings()
    assert s.session_idle_seconds == 86400
    assert s.session_absolute_seconds == 2592000
    assert s.invite_ttl_seconds == 259200
    assert s.bootstrap_open is False
    assert s.bootstrap_token is None
