"""Structural tests for the skeleton baseline migration (``0001_baseline.sql``).

Loads the packaged chain through the kit runner (the same discovery production
uses) and asserts the single-namespace connector shape, the role_audit
append-only triggers, the tool-metadata overlay, and — the migration-model
invariant — that the two marketplace version-stamp columns are folded INTO the
``CREATE TABLE`` body with no ``ALTER ... ADD COLUMN`` backfill left behind.
"""

import re

from tai42_kit.db import discover_migrations

from tai42_skeleton.db import skeleton_migrations_dir


def _baseline_sql() -> str:
    scripts = discover_migrations(skeleton_migrations_dir())
    assert scripts, "the skeleton chain must ship at least the baseline migration"
    return scripts[0].sql


def _connector_connections_block(ddl: str) -> str:
    """Return the text of the `connector_connections` CREATE TABLE (...) body."""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS connector_connections\s*\((.*?)\);",
        ddl,
        re.DOTALL,
    )
    assert match is not None, "connector_connections table not found in the baseline"
    return match.group(1)


def test_baseline_returns_nonempty_schema() -> None:
    ddl = _baseline_sql()
    assert isinstance(ddl, str)
    assert "CREATE TABLE IF NOT EXISTS connector_connections" in ddl


def test_token_store_primary_key_collapses_to_connection_id() -> None:
    block = _connector_connections_block(_baseline_sql())
    # Single-column PK on connection_id; no composite (client_name, connection_id) PK — the store is de-tenanted.
    assert re.search(r"PRIMARY KEY\s*\(\s*connection_id\s*\)", block) is not None
    assert "PRIMARY KEY (client_name" not in block


def test_token_store_has_no_tenant_columns() -> None:
    block = _connector_connections_block(_baseline_sql())
    for forbidden in ("client_name", "tenant_id", "tenant"):
        assert forbidden not in block, f"de-tenant violated: `{forbidden}` present in connector_connections"


def test_header_names_the_skeleton_baseline() -> None:
    ddl = _baseline_sql()
    assert "Nexus Platform" not in ddl
    assert "skeleton component" in ddl


def test_role_audit_append_only_triggers_present() -> None:
    """The baseline wires the three role_audit append-only triggers onto the
    versioned-document tables — the DB-level guard that a comment alone cannot give.
    Text-level guard so removing/renaming a trigger fails without a live Postgres."""
    ddl = _baseline_sql()
    # Trigger functions (idempotent CREATE OR REPLACE) and their row triggers.
    for name in (
        "versioned_document_versions_role_audit_immutable",
        "versioned_documents_role_audit_no_delete",
        "versioned_documents_role_audit_guard_update",
    ):
        assert f"CREATE OR REPLACE FUNCTION {name}()" in ddl, f"missing trigger function {name}"
        assert f"DROP TRIGGER IF EXISTS trg_{name}" in ddl, f"missing idempotent drop for trg_{name}"
        assert f"CREATE TRIGGER trg_{name}" in ddl, f"missing trigger trg_{name}"

    # The immutability trigger fires on both UPDATE and DELETE of version rows;
    # the doc-delete guard on DELETE; the update guard on UPDATE.
    assert "BEFORE UPDATE OR DELETE ON versioned_document_versions" in ddl
    assert "BEFORE DELETE ON versioned_documents" in ddl
    assert "BEFORE UPDATE ON versioned_documents" in ddl
    # Every guard keys strictly on kind='role_audit' — no other kind is affected.
    assert ddl.count("'role_audit'") >= 3


def test_marketplace_version_stamps_are_folded_into_create_table() -> None:
    """Greenfield migration model: the ``contract_version`` / ``skeleton_version``
    columns live INSIDE the ``CREATE TABLE marketplace_installs`` body, and the
    old ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` backfill (needed only for a
    pre-migration in-place upgrade) is gone. Text-level guard so a re-introduced
    ALTER — or a dropped column — fails without a live Postgres."""
    ddl = _baseline_sql()
    match = re.search(r"CREATE TABLE IF NOT EXISTS marketplace_installs\s*\((.*?)\n\);", ddl, re.DOTALL)
    assert match is not None, "marketplace_installs table not found in the baseline"
    block = match.group(1)
    for column in ("contract_version", "skeleton_version"):
        assert re.search(rf"\b{column}\s+TEXT", block) is not None, f"{column} must be in the CREATE TABLE body"
    assert "ADD COLUMN" not in ddl, "the baseline must not carry an ALTER ... ADD COLUMN backfill"


def test_marketplace_route_mounts_is_folded_into_create_table() -> None:
    """The per-item mount-base map (``route_mounts``) is a JSONB column folded INTO
    the ``CREATE TABLE marketplace_installs`` body, NOT NULL with an empty-object
    default so a row written before an operator remaps reads as "no overrides"."""
    ddl = _baseline_sql()
    match = re.search(r"CREATE TABLE IF NOT EXISTS marketplace_installs\s*\((.*?)\n\);", ddl, re.DOTALL)
    assert match is not None, "marketplace_installs table not found in the baseline"
    block = match.group(1)
    assert re.search(r"\broute_mounts\s+JSONB\s+NOT NULL\s+DEFAULT\s+'\{\}'::jsonb", block) is not None, (
        "route_mounts must be a NOT NULL JSONB column defaulting to '{}'::jsonb in the CREATE TABLE body"
    )


def test_tool_meta_overlay_tables_present() -> None:
    """The tool-metadata overlay ships two plain tables: a folder tree and a
    per-tool row. Text-level guards so a dropped/renamed table or a lost
    invariant fails without a live Postgres."""
    ddl = _baseline_sql()
    assert "CREATE TABLE IF NOT EXISTS tool_folders" in ddl
    assert "CREATE TABLE IF NOT EXISTS tool_meta" in ddl


def test_tool_folders_root_name_uniqueness_uses_nulls_not_distinct() -> None:
    """Root folders have `parent_id IS NULL`; without `NULLS NOT DISTINCT` the
    default unique would never fire for them, silently allowing duplicate root
    names. The modifier is REQUIRED on the sibling-name uniqueness constraint."""
    match = re.search(r"CREATE TABLE IF NOT EXISTS tool_folders\s*\((.*?)\n\);", _baseline_sql(), re.DOTALL)
    assert match is not None, "tool_folders table not found in the baseline"
    block = match.group(1)
    assert re.search(r"UNIQUE\s+NULLS NOT DISTINCT\s*\(\s*parent_id\s*,\s*name\s*\)", block) is not None


def test_tool_meta_has_badges_array_column() -> None:
    """`tool_meta.badges` is a NOT NULL TEXT[] defaulting to the empty array — the
    operator-overlay half of a tool's informational capability badges, folded into
    the CREATE TABLE body (no ALTER backfill). Text-level guard so a dropped/renamed
    column fails without a live Postgres."""
    match = re.search(r"CREATE TABLE IF NOT EXISTS tool_meta\s*\((.*?)\n\);", _baseline_sql(), re.DOTALL)
    assert match is not None, "tool_meta table not found in the baseline"
    block = match.group(1)
    assert re.search(r"\bbadges\s+TEXT\[\]\s+NOT NULL\s+DEFAULT\s+'\{\}'", block) is not None, (
        "badges must be a NOT NULL TEXT[] column defaulting to '{}' in the CREATE TABLE body"
    )


def test_tool_meta_hidden_is_nullable_tristate() -> None:
    """`tool_meta.hidden` is a NULLABLE boolean — the tri-state that lets NULL mean
    "defer to the plugin-declared visibility" while TRUE/FALSE force it. A
    NOT-NULL column could not express the defer state."""
    match = re.search(r"CREATE TABLE IF NOT EXISTS tool_meta\s*\((.*?)\n\);", _baseline_sql(), re.DOTALL)
    assert match is not None, "tool_meta table not found in the baseline"
    block = match.group(1)
    hidden_line = next(line for line in block.splitlines() if line.strip().startswith("hidden"))
    assert "NOT NULL" not in hidden_line


# ---------------------------------------------------------------------------
# 0002_spec_source — widen the marketplace source CHECK to admit 'spec'
# ---------------------------------------------------------------------------


def _spec_source_sql() -> str:
    scripts = discover_migrations(skeleton_migrations_dir())
    names = [(s.version, s.name) for s in scripts]
    assert (2, "spec_source") in names, f"the 0002_spec_source migration must ship in the chain; got {names}"
    return next(s.sql for s in scripts if s.version == 2)


def test_baseline_source_check_is_pypi_github_only() -> None:
    # The baseline pins the pre-descriptor set; the widening is the 0002 migration's job.
    ddl = _baseline_sql()
    assert "source IN ('pypi', 'github')" in ddl
    assert "'spec'" not in ddl


def test_spec_source_migration_widens_the_check_via_alter_on_a_populated_table() -> None:
    # The CHECK is widened by DROP CONSTRAINT + ADD CONSTRAINT — an in-place ALTER that
    # applies to an already-populated table, never a table recreate that would drop rows.
    sql = _spec_source_sql()
    assert "ALTER TABLE marketplace_installs" in sql
    assert "DROP CONSTRAINT IF EXISTS marketplace_installs_source_check" in sql
    assert re.search(r"CHECK\s*\(\s*source IN \('pypi', 'github', 'spec'\)\s*\)", sql) is not None
    # No table recreate: a widening migration must not CREATE/DROP the table.
    assert "CREATE TABLE" not in sql
    assert "DROP TABLE" not in sql
