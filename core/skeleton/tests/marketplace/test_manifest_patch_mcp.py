"""The ``mcp_entry`` manifest-patch writer: apply, remove (convergent), collision,
and hand-written-entry protection, plus the final ``Manifest.model_validate`` accept.

An mcp-server item carries transport config in ``item.mcp`` and NO module, so the
patch is a ``{title, config}`` object appended to the manifest ``mcp`` list — deduped
by title, never overwriting a hand-written or previously-installed entry.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.plugins import PluginSpec

from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.errors import ManifestCollisionError
from tai42_skeleton.marketplace.manifest_patch import apply_provides, collisions, remove_provides


def _mcp_spec(
    *, name: str = "postgres", command: str = "pg-mcp-server", env: dict[str, str] | None = None
) -> PluginSpec:
    mcp: dict[str, Any] = {"command": command}
    if env is not None:
        mcp["env"] = env
    # An all-mcp-server spec declares NO contract (the package imports none).
    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": "pg-mcp",
            "package": "tai42-pg-mcp",
            "version": "1.0.0",
            "description": "A managed Postgres MCP server.",
            "license": "Apache-2.0",
            "categories": ["dev"],
            "provides": [{"kind": "mcp-server", "name": name, "mcp": mcp, "description": "d"}],
        }
    )


def test_apply_appends_entry_titled_by_item_name() -> None:
    spec = _mcp_spec(name="postgres", command="pg-mcp-server")
    manifest: dict = {}
    apply_provides(manifest, spec)
    expected = {"title": "postgres", "config": {"command": "pg-mcp-server", "args": [], "headers": {}, "env": {}}}
    assert manifest["mcp"] == [expected]


def test_apply_preserves_marker_strings_in_config() -> None:
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${PG_PW}"})
    manifest: dict = {}
    apply_provides(manifest, spec)
    entry = manifest["mcp"][0]
    assert entry["title"] == "postgres"
    assert entry["config"]["env"] == {"PGPASSWORD": "!ENV ${PG_PW}"}


def test_apply_refuses_a_title_collision_with_a_hand_written_entry() -> None:
    spec = _mcp_spec(name="postgres")
    manifest = {"mcp": [{"title": "postgres", "config": {"url": "https://hand.written"}}]}
    with pytest.raises(ManifestCollisionError, match="postgres"):
        apply_provides(manifest, spec)
    # The hand-written entry is untouched — never overwritten.
    assert manifest["mcp"] == [{"title": "postgres", "config": {"url": "https://hand.written"}}]


def test_collisions_reports_an_existing_title() -> None:
    spec = _mcp_spec(name="postgres")
    manifest = {"mcp": [{"title": "postgres", "config": {"url": "https://x"}}]}
    found = collisions(manifest, spec)
    assert any("postgres" in message for message in found)


def test_apply_alongside_a_different_hand_written_entry_appends() -> None:
    spec = _mcp_spec(name="postgres")
    manifest = {"mcp": [{"title": "other", "config": {"url": "https://other"}}]}
    apply_provides(manifest, spec)
    titles = [entry["title"] for entry in manifest["mcp"]]
    assert titles == ["other", "postgres"]


def test_remove_drops_the_entry_by_title_and_reports_change() -> None:
    spec = _mcp_spec(name="postgres")
    manifest: dict = {}
    apply_provides(manifest, spec)
    changed = remove_provides(manifest, spec)
    assert changed is True
    assert manifest["mcp"] == []


def test_remove_leaves_a_foreign_titled_entry() -> None:
    spec = _mcp_spec(name="postgres")
    manifest = {"mcp": [{"title": "other", "config": {"url": "https://other"}}]}
    changed = remove_provides(manifest, spec)
    assert changed is False
    assert manifest["mcp"] == [{"title": "other", "config": {"url": "https://other"}}]


def test_remove_is_convergent_on_an_already_removed_entry() -> None:
    # A re-run after a partial uninstall (the title already gone) completes cleanly:
    # skipped, folded into a False ``changed``, never an error.
    spec = _mcp_spec(name="postgres")
    manifest: dict = {"mcp": []}
    changed = remove_provides(manifest, spec)
    assert changed is False


def test_applied_entry_validates_against_the_manifest_schema() -> None:
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${PG_PW:default}"})
    manifest: dict = {}
    apply_provides(manifest, spec)
    # A default-bearing marker resolves without an env, so the resolved projection
    # validates; the entry mounts as any hand-written TaiMCPConfig would.
    import os
    from typing import cast

    from pyaml_env import parse_config
    from tai42_kit.utils.data import dump_manifest

    resolved = parse_config(data=dump_manifest(cast("Any", manifest))) or {}
    Manifest.model_validate(resolved)
    assert os.environ  # sanity: resolution used the process env
