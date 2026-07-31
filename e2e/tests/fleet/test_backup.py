"""A backup exported from one deployment imports LIVE and fleet-wide onto a SECOND
deployment, both the manifest AND the env sections, with no restart.

The two sections are imported in SEPARATE calls, each with its own digest-change proof
(``POST /api/backup/import`` takes a ``sections`` name list per call). The env section's
observable is real, not vacuous: the second stack's seeded manifest references the
imported env key via ``!ENV``, so applying the env moves the RESOLVED live view — the
digest — fleet-wide. The manifest section then replaces the live manifest, adding the
source's mcp entry fleet-wide (it shows in ``GET /api/manifest``). Finally a CORRUPTED
manifest document is imported: its section report carries the error and the persisted
manifest is byte-identical (nothing partial lands).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import yaml
from _fleet import (
    build_backup_source_stack,
    build_fleet_env_stack_builder,
    converged_baseline,
    converged_digest,
    manifest_file,
)

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

# The env key the source writes and the second stack's manifest references via ``!ENV``.
_KEY = "E2E_FLEET_B4"
_UNREACHABLE_MCP = {"config": {"url": "http://127.0.0.1:1/mcp"}}


async def _manifest_mcp_titles(stack: TaiStack) -> list[str]:
    manifest = await stack.api().get("/api/manifest", retry_on_reloading=True)
    return [entry["title"] for entry in manifest["mcp"]]


async def test_backup_import_applies_manifest_and_env_fleet_wide(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    # -- source: write the env key and add an mcp entry, then export both sections.
    source = fresh_stack(build_backup_source_stack)
    await source.api().post("/api/config/env", json={_KEY: "imported-b4-value"}, retry_on_reloading=True)
    mcp_title = uniq("b4_mcp")
    src_doc = yaml.safe_load(manifest_file(source).read_text()) or {}
    src_doc["mcp"] = [*src_doc.get("mcp", []), {"title": mcp_title, **_UNREACHABLE_MCP}]
    await source.api().post(
        "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(src_doc)}, retry_on_reloading=True
    )
    # The export door is a raw downloadable document (NOT the ``{"data": ...}`` envelope).
    export = await source.api().request_raw("POST", "/api/backup/export", json={"sections": ["env", "manifest"]})
    assert export.status_code == 200, export.text
    document = export.json()
    assert _KEY in document["sections"]["env"], f"the env section did not carry the written key: {document}"

    # -- second stack: its seeded manifest references _KEY via ``!ENV``.
    target = fresh_stack(build_fleet_env_stack_builder(_KEY))
    api = target.api()
    baseline = await converged_baseline(target)

    # Import #1 — env only: the ``!ENV``-referenced marker now resolves to the imported
    # value, so the resolved live view moves fleet-wide.
    r_env = await api.post(
        "/api/backup/import", json={"document": document, "sections": ["env"]}, retry_on_reloading=True
    )
    assert r_env["ok"], f"env import failed: {r_env}"
    after_env = await converged_digest(target, differ_from=baseline)

    # Import #2 — manifest only: the persisted manifest is replaced fleet-wide, adding
    # the source's mcp entry.
    r_man = await api.post(
        "/api/backup/import", json={"document": document, "sections": ["manifest"]}, retry_on_reloading=True
    )
    assert r_man["ok"], f"manifest import failed: {r_man}"
    await converged_digest(target, differ_from=after_env)
    assert mcp_title in await _manifest_mcp_titles(target)  # the imported entry is live fleet-wide


async def test_corrupted_manifest_import_reports_error_and_persists_nothing(
    fresh_stack: Callable[..., TaiStack],
) -> None:
    """Corrupt import: a malformed manifest section is reported as a section error
    and the persisted manifest is byte-identical — nothing partial lands."""
    target = fresh_stack(build_backup_source_stack)
    api = target.api()
    before = manifest_file(target).read_bytes()

    # ``tools`` must be a list; a string fails the ``Manifest`` schema inside the
    # pipeline's replace, so the importer imports nothing for the section.
    corrupt = {"version": 1, "sections": {"manifest": {"tools": "not-a-list"}}, "errors": {}}
    result = await api.post(
        "/api/backup/import", json={"document": corrupt, "sections": ["manifest"]}, retry_on_reloading=True
    )
    assert result["ok"] is False, f"a corrupted manifest import must not report ok: {result}"
    assert result["sections"]["manifest"]["errors"], f"the section report carried no error: {result}"
    assert manifest_file(target).read_bytes() == before, "a failed manifest import must not have touched the manifest"
