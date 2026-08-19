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

from collections.abc import Callable, Iterator

import pytest
import yaml

from tai42_e2e.booting import boot_stack
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import Infra, TaiStack

from ._fleet import (
    build_backup_populated_stack,
    build_backup_source_stack,
    build_fleet_env_stack_builder,
    converged_baseline,
    converged_digest,
    manifest_file,
)

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


async def test_backup_sub_mcp_import_mode_matrix_on_populated_instance(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """The backup import per-record MODE matrix on a POPULATED instance, over the
    keyed ``sub_mcp`` section (slug is the natural key). A slug is registered and
    exported, then DRIFTED (its live tools list re-registered to a different set).
    Re-importing that same backup exercises both modes on the record that now exists:

    * ``skip`` (the default) LEAVES the drifted live record untouched and reports it
      under ``skipped_existing`` — a clean skip, never an error, and never a
      created/updated — so a re-import of an unchanged backup cannot clobber drift;
    * ``overwrite`` UPSERTS the record back to the backup's tools and reports it under
      ``updated``.

    This is the keyed-record contract every mode-aware section (conversations, tokens,
    templates, ...) shares; the section-specific legs (token skip-only, conversations
    callback-secret non-remint) are exercised on a populated instance by
    ``test_skip_import_preserves_conversation_secret_and_token`` below.
    """
    stack = fresh_stack(build_backup_source_stack)
    api = stack.api()
    slug = uniq("submcp").replace("_", "-")

    # -- populate: register a sub-MCP app over a real probe tool, then export the section.
    await api.post("/api/sub-mcp", json={"slug": slug, "tools": ["e2e_echo"]}, retry_on_reloading=True)
    export = await api.request_raw("POST", "/api/backup/export", json={"sections": ["sub_mcp"]})
    assert export.status_code == 200, export.text
    document = export.json()
    assert document["sections"]["sub_mcp"][slug]["tools"] == ["e2e_echo"], document

    # -- drift: re-register the SAME slug with a different tools set (POST upserts the store).
    await api.post("/api/sub-mcp", json={"slug": slug, "tools": ["e2e_echo", "e2e_fail"]}, retry_on_reloading=True)
    drifted = await api.get("/api/sub-mcp", retry_on_reloading=True)
    assert drifted[slug]["tools"] == ["e2e_echo", "e2e_fail"], drifted

    # -- skip: the drifted record is left standing and reported skipped_existing.
    skip = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["sub_mcp"], "mode": "skip"},
        retry_on_reloading=True,
    )
    assert skip["ok"], skip
    report = skip["sections"]["sub_mcp"]
    assert report["skipped_existing"] == 1, report
    assert report["created"] == 0, report
    assert report["updated"] == 0, report
    assert report["errors"] == [], report
    after_skip = await api.get("/api/sub-mcp", retry_on_reloading=True)
    assert after_skip[slug]["tools"] == ["e2e_echo", "e2e_fail"], "skip must not overwrite the drifted record"

    # -- overwrite: the record is upserted back to the backup's tools and reported updated.
    over = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["sub_mcp"], "mode": "overwrite"},
        retry_on_reloading=True,
    )
    assert over["ok"], over
    report = over["sections"]["sub_mcp"]
    assert report["updated"] == 1, report
    assert report["created"] == 0, report
    assert report["skipped_existing"] == 0, report
    assert report["errors"] == [], report
    after_over = await api.get("/api/sub-mcp", retry_on_reloading=True)
    assert after_over[slug]["tools"] == ["e2e_echo"], "overwrite must restore the backup's record fleet-wide"


@pytest.fixture(scope="module")
def backup_populated_stack(
    infra: Infra, tmp_path_factory: pytest.TempPathFactory, llm_stub: LlmStub
) -> Iterator[TaiStack]:
    """The access-control-ON backup stack, seeded with a root key BEFORE boot (readiness
    is denied until the route table pins the probes public), booted once for the module.
    It carries the redis conversations backend + the api-key store, so a real route and a
    real token can be populated and round-tripped through a backup."""
    yield from boot_stack(
        infra,
        tmp_path_factory.mktemp("backup_populated"),
        build_backup_populated_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        seed_auth=True,
    )


async def test_skip_import_preserves_conversation_secret_and_token(
    backup_populated_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A ``skip`` re-import over a POPULATED instance re-mints NOTHING.

    The instance holds an existing ``api``-door conversation route (its ``callback_secret``
    minted on the host) and an existing api-key token (the route's execution key). Both
    sections are exported, the backup is then EDITED to carry changed values, and it is
    re-imported in ``skip`` mode. Skip must ignore the edits and leave both records exactly
    as they stand:

    * the conversation route is ``skipped_existing`` and its callback secret is NOT
      re-minted (a re-mint would surface in ``new_callback_secrets``) — matching
      ``conversations/backup.py`` (no re-mint on skip);
    * the token is ``skipped_existing`` and NOT re-minted (a mint would surface in
      ``new_api_keys``) — matching ``access_control`` tokens being STRUCTURALLY skip-only.
    """
    stack = backup_populated_stack
    api = stack.api()

    # -- populate: an api-key token that doubles as the route's execution key, then an
    # api-door route whose callback_secret is minted on the host and shown once.
    exec_key = uniq("bkp-exec").replace("_", "-")
    minted = await api.post(
        "/api/auth/api-keys",
        json={"user_id": exec_key, "description": "backup-skip token", "scopes": ["e2e-all"]},
        retry_on_reloading=True,
    )
    assert isinstance(minted, dict), minted
    assert minted["api_key"].startswith("sk-"), minted

    route_name = uniq("bkp-route").replace("_", "-")
    created = await api.post(
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "agent",
            "target_name": "tools_agent",
            "execution_key": exec_key,
            "callback_url": "https://127.0.0.1:9/callback",
        },
        retry_on_reloading=True,
    )
    assert created["callback_secret"], created  # minted once here; reads withhold it

    # -- export both sections. The export withholds the live callback secret (import
    # re-mints it ONLY under overwrite), so a skip re-import cannot re-mint from it.
    export = await api.request_raw("POST", "/api/backup/export", json={"sections": ["access_control", "conversations"]})
    assert export.status_code == 200, export.text
    document = export.json()
    routes = document["sections"]["conversations"]["routes"]
    assert route_name in [route["route_name"] for route in routes], document
    assert all("callback_secret" not in route for route in routes), routes
    assert any(token["user_id"] == exec_key for token in document["sections"]["access_control"]["tokens"]), document

    # -- EDIT the backup to carry changed values, then import it in skip mode. Skip must
    # ignore every edit and leave the live records untouched.
    for route in document["sections"]["conversations"]["routes"]:
        if route["route_name"] == route_name:
            route["callback_url"] = "https://127.0.0.1:9/CHANGED"
    for token in document["sections"]["access_control"]["tokens"]:
        if token["user_id"] == exec_key:
            token["description"] = "CHANGED — skip must ignore this"

    result = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["access_control", "conversations"], "mode": "skip"},
        retry_on_reloading=True,
    )
    assert result["ok"], result

    # (a) the conversation route is skipped and its callback secret is NOT re-minted.
    conv = result["sections"]["conversations"]
    assert conv["skipped_existing"] == 1, conv
    assert conv["created"] == 0, conv
    assert conv["updated"] == 0, conv
    assert conv["new_callback_secrets"] == [], conv  # a re-mint would surface here
    assert conv["errors"] == [], conv

    # (b) the token is left untouched — skipped_existing, never re-minted (skip-only).
    ac = result["sections"]["access_control"]
    assert ac["created"] == 0, ac
    assert ac["updated"] == 0, ac
    assert ac["new_api_keys"] == [], ac  # a token mint would surface here
    assert ac["skipped_existing"] >= 1, ac
    assert ac["errors"] == [], ac


async def test_backup_import_env_x_band_key_is_refused(fresh_stack: Callable[..., TaiStack]) -> None:
    """A crafted backup whose env section carries a deployment X-band key (``TAI_SUPERVISED``)
    is REFUSED loudly: backup env restore rides ``apply_env_change`` (``backup/sections.py``),
    so the shared boundary validator fires there with no backup-specific code. The
    section report carries the error naming the offending key, and nothing lands — the X-band
    value never reaches the store, nor does the sibling key in the same (atomically refused)
    section (validate-before-write)."""
    target = fresh_stack(build_backup_source_stack)
    api = target.api()
    before = (await api.get("/api/config/env", retry_on_reloading=True))["env"]
    assert "TAI_SUPERVISED" not in before

    document = {
        "version": 1,
        "sections": {"env": {"E2E_BACKUP_OK": "harmless", "TAI_SUPERVISED": "spoofed-shape"}},
        "errors": {},
    }
    result = await api.post(
        "/api/backup/import", json={"document": document, "sections": ["env"]}, retry_on_reloading=True
    )
    assert result["ok"] is False, f"an X-band backup env import must not report ok: {result}"
    errors = result["sections"]["env"]["errors"]
    assert errors, f"the env section report carried no error: {result}"
    assert any("TAI_SUPERVISED" in str(err) for err in errors), f"the refusal did not name the X-band key: {errors}"

    # Nothing landed — the whole section was refused before any write.
    after = (await api.get("/api/config/env", retry_on_reloading=True))["env"]
    assert "TAI_SUPERVISED" not in after, f"the X-band value leaked into the stored env: {after}"
    assert after.get("E2E_BACKUP_OK") != "harmless", f"a refused section still wrote a sibling key: {after}"


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
