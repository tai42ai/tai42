"""Connection-settings composed by the pooled clients: the pool-key kwargs are
JSON-serializable and carry only the connection identity."""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import SecretStr, ValidationError

from tai42_kit.clients.settings import PostgresConnectionSettings, RedisConnectionSettings


class TestRedisConnectionSettings:
    def test_minimal_kwargs_are_serializable(self):
        kwargs = RedisConnectionSettings(redis_url="redis://localhost:6379/0").client_kwargs()
        assert kwargs == {"url": "redis://localhost:6379/0", "max_connections": None, "decode_responses": True}
        json.dumps(kwargs)  # pool key must serialize

    def test_resilience_fields_are_opt_in(self):
        base = RedisConnectionSettings(redis_url="redis://x").client_kwargs()
        assert "socket_timeout" not in base
        assert "retry_on_timeout" not in base
        assert "retry_attempts" not in base

        tuned = RedisConnectionSettings(
            redis_url="redis://x", socket_timeout=1.5, retry_on_timeout=True, retry_attempts=3
        ).client_kwargs()
        assert tuned["socket_timeout"] == 1.5
        assert tuned["retry_on_timeout"] is True
        assert tuned["retry_attempts"] == 3
        json.dumps(tuned)

    def test_socket_connect_timeout_is_opt_in(self):
        # Unset -> absent from the pool key (bare from_url behavior preserved).
        base = RedisConnectionSettings(redis_url="redis://x").client_kwargs()
        assert "socket_connect_timeout" not in base

        tuned = RedisConnectionSettings(redis_url="redis://x", socket_connect_timeout=2.5).client_kwargs()
        assert tuned["socket_connect_timeout"] == 2.5
        json.dumps(tuned)

    def test_socket_connect_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            RedisConnectionSettings(redis_url="redis://x", socket_connect_timeout=0)


class TestPostgresConnectionSettings:
    def test_dsn_is_built_from_fields(self):
        settings = PostgresConnectionSettings(
            pg_host="db", pg_port=5433, pg_db="app", pg_user="u", pg_password=SecretStr("p")
        )
        assert settings.pg_dsn == (
            "postgresql://u:p@db:5433/app?connect_timeout=10&options=-c%20statement_timeout%3D60000"
        )

    def test_pg_password_is_masked_outside_the_dsn(self):
        # SecretStr keeps the password out of repr/logs/serialization; the
        # plaintext appears only in the driver DSN. This guards against a silent
        # downgrade back to a plain ``str`` field.
        settings = PostgresConnectionSettings(pg_user="u", pg_password=SecretStr("s3cr3t"))
        assert "s3cr3t" not in repr(settings)
        assert "s3cr3t" not in settings.model_dump_json()
        assert "s3cr3t" in settings.pg_dsn

    def test_dsn_encodes_reserved_password_chars_and_round_trips(self):
        # Reserved characters in the password (@ / : # ?) are percent-encoded,
        # so the DSN's netloc still targets the configured host and a standard
        # urlsplit + unquote recovers the original credentials exactly.
        settings = PostgresConnectionSettings(
            pg_host="db", pg_port=5433, pg_db="app", pg_user="u", pg_password=SecretStr("p@ss:w/rd#?")
        )
        parts = urlsplit(settings.pg_dsn)
        assert parts.hostname == "db"
        assert parts.port == 5433
        assert parts.path == "/app"
        assert parts.username is not None
        assert unquote(parts.username) == "u"
        assert parts.password is not None
        assert unquote(parts.password) == "p@ss:w/rd#?"

    def test_dsn_brackets_ipv6_host(self):
        # An IPv6 host literal is bracketed so its colons are not parsed as the
        # port separator; urlsplit then recovers the address and the port.
        settings = PostgresConnectionSettings(pg_host="::1", pg_port=5432, pg_db="app", pg_user="u")
        assert "@[::1]:5432/" in settings.pg_dsn
        parts = urlsplit(settings.pg_dsn)
        assert parts.hostname == "::1"
        assert parts.port == 5432

    def test_dsn_encodes_reserved_db_chars(self):
        # A db name with reserved characters is percent-encoded so it stays a
        # single path segment rather than opening a new path/query.
        settings = PostgresConnectionSettings(pg_host="db", pg_db="my/db?x", pg_user="u")
        parts = urlsplit(settings.pg_dsn)
        # The db's reserved chars are percent-encoded, so it stays a single path
        # segment; the only query component is the appended libpq timeout params.
        assert unquote(parts.path.lstrip("/")) == "my/db?x"
        assert parts.query.startswith("connect_timeout=")

    def test_client_kwargs_carry_dsn_and_bounds(self):
        # No baked-in credential: the default password is empty.
        kwargs = PostgresConnectionSettings(pg_min_connections=1, pg_max_connections=4).client_kwargs()
        assert kwargs == {
            "dsn": "postgresql://postgres:@localhost:5432/postgres?connect_timeout=10&options=-c%20statement_timeout%3D60000",
            "min_size": 1,
            "max_size": 4,
        }
        json.dumps(kwargs)

    def test_dsn_carries_default_connect_and_statement_timeouts(self):
        # Default 10s connect + 60s statement -> connect_timeout=10 and a
        # percent-encoded ``-c statement_timeout=60000`` (whole ms).
        dsn = PostgresConnectionSettings(pg_user="u").pg_dsn
        assert "connect_timeout=10" in dsn
        assert "options=-c%20statement_timeout%3D60000" in dsn
        # The options value survives a standard query parse back to the raw form.
        query = parse_qs(urlsplit(dsn).query)
        assert query["connect_timeout"] == ["10"]
        assert query["options"] == ["-c statement_timeout=60000"]

    def test_dsn_statement_timeout_converts_seconds_to_integer_ms(self):
        # 1.5s -> 1500ms, emitted as an integer (no fractional milliseconds).
        dsn = PostgresConnectionSettings(pg_user="u", pg_statement_timeout_seconds=1.5).pg_dsn
        assert "options=-c%20statement_timeout%3D1500" in dsn
        assert parse_qs(urlsplit(dsn).query)["options"] == ["-c statement_timeout=1500"]

    def test_dsn_sub_millisecond_statement_timeout_never_disables_the_bound(self):
        # A positive sub-millisecond value (gt=0 admits it) must round UP to 1 ms, not
        # floor to ``statement_timeout=0`` — which Postgres reads as DISABLED
        # (unbounded), the opposite of a configured bound.
        dsn = PostgresConnectionSettings(pg_user="u", pg_statement_timeout_seconds=0.0009).pg_dsn
        assert parse_qs(urlsplit(dsn).query)["options"] == ["-c statement_timeout=1"]

    def test_dsn_respects_custom_connect_timeout(self):
        dsn = PostgresConnectionSettings(pg_user="u", pg_connect_timeout=3).pg_dsn
        assert parse_qs(urlsplit(dsn).query)["connect_timeout"] == ["3"]

    def test_non_positive_timeouts_are_rejected(self):
        with pytest.raises(ValidationError):
            PostgresConnectionSettings(pg_connect_timeout=0)
        with pytest.raises(ValidationError):
            PostgresConnectionSettings(pg_statement_timeout_seconds=0)
