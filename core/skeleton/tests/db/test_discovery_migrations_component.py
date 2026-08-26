"""The ``migrations_component`` override in plugin chain discovery.

An override chain migrates the NAMED database component (its history identity AND
its connection) instead of the distribution — but ONLY while that component's
``TAI_DB_BINDING_*`` is EXPLICITLY declared. Unset, the chain is a VISIBLE skip
(a surfaced log line, no entry), never the default-database fallback and never the
silent ``None`` no-migrations outcome. With no override the component is the
distribution name, byte-identical to prior behavior.

No live Postgres — pure resolution against installed package metadata. The
``tai42-skeleton`` distribution is guaranteed installed, so its real ``sql/migrations``
directory doubles as the packaged fixture chain. Generic component/binding names
(``acme_alerts`` / ``alertsdb``) stand in for an off-unless-declared feature store.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from tai42_contract.plugins import PluginSpec

from tai42_skeleton.db import installed_plugin_entries, plugin_migration_entry
from tai42_skeleton.db.discovery import _ChainSkip, _plugin_chain

_DISCOVERY_LOGGER = "tai42_skeleton.db.discovery"


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default database is configured by a password alone; the migrator identity
    # then resolves (no connection is opened while building entries). A distinct host
    # marks the DEFAULT database so a mis-bound override chain is caught.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "default-host")


def _spec(*, migrations: str | None, migrations_component: str | None = None) -> PluginSpec:
    body: dict[str, object] = {
        "spec_version": 1,
        "namespace": "tai42",
        "name": "widget",
        "package": "tai42-skeleton",
        "version": "1.0.0",
        "description": "A test plugin",
        "license": "Apache-2.0",
        "contract": ">=0.1,<1.0",
        "categories": ["dev"],
        "provides": [{"kind": "tool", "name": "w", "module": "w.m", "description": "d"}],
        "migrations": migrations,
    }
    if migrations_component is not None:
        body["migrations_component"] = migrations_component
    return PluginSpec.model_validate(body)


def _bind_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Declare the override component's binding (its store lives in a SEPARATE
    # database) and configure that database with a marker host.
    monkeypatch.setenv("TAI_DB_BINDING_ACME_ALERTS", "alertsdb")
    monkeypatch.setenv("TAI_DATABASE_ALERTSDB_PG_PASSWORD", "secret")
    monkeypatch.setenv("TAI_DATABASE_ALERTSDB_PG_HOST", "alerts-host")


def test_override_runs_against_the_declared_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_alerts(monkeypatch)
    spec = _spec(migrations="sql/migrations", migrations_component="acme_alerts")

    entry = plugin_migration_entry(spec)
    assert entry is not None
    # The history identity is the OVERRIDE component, not the distribution.
    assert entry.component == "acme_alerts"
    # ...and its CONNECTION points at that component's bound database, not the default.
    assert entry.settings.pg_host == "alerts-host"
    assert entry.migrations_dir.is_dir()


def test_override_with_unset_binding_is_a_visible_skip(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The binding is NOT declared: the chain must be skipped (no entry) and the skip
    # surfaced by a visible line naming the chain and its undeclared binding — never
    # migrated into the default database by fallback.
    monkeypatch.delenv("TAI_DB_BINDING_ACME_ALERTS", raising=False)
    spec = _spec(migrations="sql/migrations", migrations_component="acme_alerts")

    # The three-outcome resolver returns the DISTINCT skip sentinel, not None/entry.
    assert _plugin_chain(spec) == _ChainSkip(component="acme_alerts")

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        entry = plugin_migration_entry(spec)

    # No migration entry is applied for the skipped chain.
    assert entry is None
    # The surfaced line names WHICH chain did not run and WHY (its binding env).
    skip_lines = [r.getMessage() for r in caplog.records if r.name == _DISCOVERY_LOGGER]
    assert any("acme_alerts" in msg and "TAI_DB_BINDING" in msg for msg in skip_lines)


def test_visible_skip_is_distinct_from_silent_no_migrations(caplog: pytest.LogCaptureFixture) -> None:
    # Both a no-migrations plugin AND an unset-binding override resolve to a None
    # entry — but they are DISTINCT: the no-migrations path is silent, the override
    # skip is surfaced. The visible line is what separates them.
    no_migrations = _spec(migrations=None)
    override_skip = _spec(migrations="sql/migrations", migrations_component="acme_alerts")

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        assert plugin_migration_entry(no_migrations) is None
    assert [r for r in caplog.records if r.name == _DISCOVERY_LOGGER] == []  # silent

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        assert plugin_migration_entry(override_skip) is None
    assert any(r.name == _DISCOVERY_LOGGER for r in caplog.records)  # surfaced


def test_no_override_chain_is_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    # With no override the component is the distribution name and the chain resolves
    # WITHOUT any TAI_DB_BINDING_* declared — byte-identical to prior behavior, and
    # never a skip.
    spec = _spec(migrations="sql/migrations")

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        entry = plugin_migration_entry(spec)

    assert entry is not None
    assert entry.component == "tai42-skeleton"  # the distribution identity
    assert entry.settings.pg_host == "default-host"  # bound to the default database
    assert entry.migrations_dir.is_dir()
    # An ordinary chain never surfaces a skip line.
    assert [r for r in caplog.records if r.name == _DISCOVERY_LOGGER] == []


async def test_installed_entries_omit_and_surface_the_skip(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Across the marketplace store: an unset-binding override chain is omitted from
    # the runner entries AND surfaced, while an ordinary chain is included — the skip
    # is reported, never silently dropped.
    from tai42_skeleton.marketplace import store as mp_store

    monkeypatch.delenv("TAI_DB_BINDING_ACME_ALERTS", raising=False)
    override_skip = _spec(migrations="sql/migrations", migrations_component="acme_alerts").model_dump(mode="json")
    plain_chain = _spec(migrations="sql/migrations").model_dump(mode="json")

    class _FakeStore:
        async def list_installed(self):
            return [SimpleNamespace(spec=override_skip), SimpleNamespace(spec=plain_chain)]

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _FakeStore)

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        entries = await installed_plugin_entries()

    # Only the ordinary chain contributes an entry; the override skip is omitted.
    assert [entry.component for entry in entries] == ["tai42-skeleton"]
    # ...and the omission is surfaced, naming the skipped chain.
    assert any(r.name == _DISCOVERY_LOGGER and "acme_alerts" in r.getMessage() for r in caplog.records)
