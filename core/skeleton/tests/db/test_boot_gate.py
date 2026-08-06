"""The boot-time schema gate (A3): refuse to serve on an out-of-date database.

The refusal itself is kit's shared ``assert_chain_applied`` primitive (covered in
kit's own tests); these tests pin the skeleton's WIRING around it — its feature
collection, the DSN de-duplication, the exact ``tai db migrate`` remediation it
supplies, and that the gate is a no-op when no schema-owning feature is configured.
One test drives the real kit primitive end-to-end (faking only kit's
``migration_status``) so the byte-exact refusal wording stays pinned here too.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace

import pytest
import tai42_kit.db.migrations as kit_migrations
from tai42_kit.db import ComponentStatus, MigrationEntry, MigrationScript

from tai42_skeleton.db import boot_gate
from tai42_skeleton.db.boot_gate import SchemaOutOfDateError, assert_chain_applied, assert_skeleton_schema_applied
from tai42_skeleton.db.boot_gate import _skeleton_schema_features as _real_skeleton_schema_features


def _script(version: int, name: str) -> MigrationScript:
    return MigrationScript(version=version, name=name, filename=f"{version:04d}_{name}.sql", checksum="c", sql="")


def _status(component: str, *, pending: tuple = (), mismatches: tuple = ()) -> ComponentStatus:
    return ComponentStatus(component=component, applied_versions=(1,), pending=pending, mismatches=mismatches)


def test_gate_re_exports_the_kit_primitive() -> None:
    # Importers reaching for the gate's ``assert_chain_applied`` / ``SchemaOutOfDateError``
    # get the single kit definitions, not a skeleton-local duplicate.
    assert assert_chain_applied is kit_migrations.assert_chain_applied
    assert SchemaOutOfDateError is kit_migrations.SchemaOutOfDateError


async def test_skeleton_gate_noop_when_no_feature_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        boot_gate, "_skeleton_schema_features", lambda: [(lambda: False, lambda: SimpleNamespace(pg_dsn="unused"))]
    )

    async def _forbidden(entries: Sequence[MigrationEntry], *, remediation: str) -> None:
        raise AssertionError("an all-off deployment must not run the schema gate")

    monkeypatch.setattr(boot_gate, "assert_chain_applied", _forbidden)
    await assert_skeleton_schema_applied()  # no raise, no query


async def test_skeleton_gate_checks_configured_feature_and_supplies_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Drive the REAL kit primitive (only kit's ``migration_status`` is faked) so the
    # skeleton's remediation wording and the byte-exact refusal are pinned end-to-end.
    monkeypatch.setattr(
        boot_gate, "_skeleton_schema_features", lambda: [(lambda: True, lambda: SimpleNamespace(pg_dsn="dsn1"))]
    )

    async def _fn(entries: Sequence[MigrationEntry]) -> list[ComponentStatus]:
        assert len(entries) == 1
        return [_status("skeleton", pending=(_script(2, "add_thing"),))]

    monkeypatch.setattr(kit_migrations, "migration_status", _fn)
    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await assert_skeleton_schema_applied()
    message = str(excinfo.value)
    assert message == (
        "database schema is out of date — refusing to start: skeleton: 1 pending (0002_add_thing.sql). "
        "Run 'tai db migrate' to apply the pending migrations, then restart."
    )


async def test_skeleton_gate_dedups_by_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    features = [
        (lambda: True, lambda: SimpleNamespace(pg_dsn="same")),
        (lambda: True, lambda: SimpleNamespace(pg_dsn="same")),
        (lambda: True, lambda: SimpleNamespace(pg_dsn="other")),
    ]
    monkeypatch.setattr(boot_gate, "_skeleton_schema_features", lambda: features)
    captured: dict[str, object] = {}

    async def _fn(entries: Sequence[MigrationEntry], *, remediation: str) -> None:
        captured["count"] = len(entries)
        captured["remediation"] = remediation

    monkeypatch.setattr(boot_gate, "assert_chain_applied", _fn)
    await assert_skeleton_schema_applied()
    assert captured["count"] == 2  # two distinct DSNs, the duplicate collapsed
    # The skeleton supplies its own fix wording; kit hard-codes none.
    assert captured["remediation"] == "Run 'tai db migrate' to apply the pending migrations, then restart."


def test_real_skeleton_schema_features_registers_the_five_stores() -> None:
    # The real registry (the autouse offline fixture patches the module attr, so the
    # bound reference captured at import drives the real body). Every entry is a
    # (predicate, settings-factory) pair the gate can call.
    features = _real_skeleton_schema_features()
    assert len(features) == 5
    for predicate, settings_factory in features:
        assert callable(predicate)
        assert callable(settings_factory)
