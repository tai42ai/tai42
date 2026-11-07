"""A backup exported from one deployment imports LIVE onto every worker of a SECOND
deployment, both the manifest AND the env sections, with no restart.

The two sections are imported in SEPARATE calls (``POST /api/backup/import`` takes a
``sections`` name list per call), each proven by its section's own fan-out report: every
serve member on the bus census at the time of the call has an ``applied`` entry and no
entry is anything else. The second stack's seeded manifest references the imported env key
via ``!ENV``, so applying the env changes the RESOLVED config every worker reloads. The
manifest section then replaces the live manifest, adding the source's mcp entry (it is
served in ``GET /api/manifest``). Finally a CORRUPTED
manifest document is imported: its section report carries the error and the persisted
manifest is byte-identical (nothing partial lands).
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable, Iterator

import pytest
import yaml

from tai42_e2e import Infra
from tai42_e2e.booting import boot_stack
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

from ._fleet import (
    assert_applied_by,
    assert_fleet_fanout,
    build_backup_definitions_stack,
    build_backup_populated_stack,
    build_backup_source_stack,
    build_backup_storage_stack,
    build_fleet_env_stack_builder,
    manifest_file,
    serve_members,
)

pytestmark = [pytest.mark.backendless, pytest.mark.needs("setting:router:backup")]

# The env key the source writes and the second stack's manifest references via ``!ENV``.
_KEY = "E2E_FLEET_B4"
_UNREACHABLE_MCP = {"config": {"url": "http://127.0.0.1:1/mcp"}}


async def _manifest_mcp_titles(stack: TaiStack) -> list[str]:
    manifest = await stack.api().get("/api/manifest")
    return [entry["title"] for entry in manifest["mcp"]]


# A liveness bound (a bound on a hang, not a performance limit): twice the slowest legitimate
# run, both stack boots included, measured on CI (arq, shard 4, 2026-11-06: 29.2s), rounded up to the
# next 10s.
@pytest.mark.timeout(60)
@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "process",
    "files",
    "topology:multiworker",
    "setting:two-stacks",
    "setting:manifest:mcp-env-probe",
)
async def test_backup_import_applies_manifest_and_env_fleet_wide(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    # -- source: write the env key and add an mcp entry, then export both sections.
    source = fresh_stack(build_backup_source_stack)
    await source.api().post("/api/config/env", json={"env": {_KEY: "imported-b4-value"}})
    mcp_title = uniq("b4_mcp")
    src_doc = yaml.safe_load(manifest_file(source).read_text()) or {}
    src_doc["mcp"] = [*src_doc.get("mcp", []), {"title": mcp_title, **_UNREACHABLE_MCP}]
    await source.api().post("/api/manifest/replace", json={"manifest_text": yaml.safe_dump(src_doc)})
    # The export door is a raw downloadable document (NOT the ``{"data": ...}`` envelope).
    export = await source.api().request_raw("POST", "/api/backup/export", json={"sections": ["env", "manifest"]})
    assert export.status_code == 200, export.text
    document = export.json()
    assert _KEY in document["sections"]["env"], f"the env section did not carry the written key: {document}"

    # -- second stack: its seeded manifest references _KEY via ``!ENV``.
    target = fresh_stack(build_fleet_env_stack_builder(_KEY))
    api = target.api()

    # Import #1 — env only: the ``!ENV``-referenced marker now resolves to the imported
    # value, and every serve member reloads it.
    members = serve_members(target)
    r_env = await api.post("/api/backup/import", json={"document": document, "sections": ["env"]})
    assert r_env["ok"], f"env import failed: {r_env}"
    assert_applied_by(assert_fleet_fanout(r_env["sections"]["env"]), members)

    # Import #2 — manifest only: every serve member applies the replaced manifest, adding
    # the source's mcp entry.
    members = serve_members(target)
    r_man = await api.post("/api/backup/import", json={"document": document, "sections": ["manifest"]})
    assert r_man["ok"], f"manifest import failed: {r_man}"
    assert_applied_by(assert_fleet_fanout(r_man["sections"]["manifest"]), members)
    titles = await _manifest_mcp_titles(target)
    assert mcp_title in titles, f"the imported entry is not served: {titles}"


@pytest.mark.needs("probe-tools", "process")
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
    storage, ...) shares; the section-specific legs (token skip-only, conversations
    callback-secret non-remint) are exercised on a populated instance by
    ``test_skip_import_preserves_conversation_secret_and_token`` below.
    """
    stack = fresh_stack(build_backup_source_stack)
    api = stack.api()
    slug = uniq("submcp").replace("_", "-")

    # -- populate: register a sub-MCP app over a real probe tool, then export the section.
    await api.post("/api/sub-mcp", json={"slug": slug, "tools": ["e2e_echo"]})
    export = await api.request_raw("POST", "/api/backup/export", json={"sections": ["sub_mcp"]})
    assert export.status_code == 200, export.text
    document = export.json()
    assert document["sections"]["sub_mcp"][slug]["tools"] == ["e2e_echo"], document

    # -- drift: re-register the SAME slug with a different tools set (POST upserts the store).
    await api.post("/api/sub-mcp", json={"slug": slug, "tools": ["e2e_echo", "e2e_fail"]})
    drifted = await api.get("/api/sub-mcp")
    assert drifted[slug]["tools"] == ["e2e_echo", "e2e_fail"], drifted

    # -- skip: the drifted record is left standing and reported skipped_existing.
    skip = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["sub_mcp"], "mode": "skip"},
    )
    assert skip["ok"], skip
    report = skip["sections"]["sub_mcp"]
    assert report["details"]["skipped_existing"] == 1, report
    assert report["created"] == 0, report
    assert report["updated"] == 0, report
    assert report["errors"] == [], report
    after_skip = await api.get("/api/sub-mcp")
    assert after_skip[slug]["tools"] == ["e2e_echo", "e2e_fail"], "skip must not overwrite the drifted record"

    # -- overwrite: the record is upserted back to the backup's tools and reported updated.
    over = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["sub_mcp"], "mode": "overwrite"},
    )
    assert over["ok"], over
    report = over["sections"]["sub_mcp"]
    assert report["updated"] == 1, report
    assert report["created"] == 0, report
    assert report["details"]["skipped_existing"] == 0, report
    assert report["errors"] == [], report
    after_over = await api.get("/api/sub-mcp")
    assert after_over[slug]["tools"] == ["e2e_echo"], "overwrite must restore the backup's record"


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


@pytest.mark.needs(
    "kind:identity",
    "setting:seeded-access-control",
    "setting:conversations:redis",
    "setting:agent:tools_agent",
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
    )
    assert result["ok"], result

    # (a) the conversation route is skipped and its callback secret is NOT re-minted.
    conv = result["sections"]["conversations"]
    assert conv["details"]["skipped_existing"] == 1, conv
    assert conv["created"] == 0, conv
    assert conv["updated"] == 0, conv
    assert conv["details"]["new_callback_secrets"] == [], conv  # a re-mint would surface here
    assert conv["errors"] == [], conv

    # (b) the token is left untouched — skipped_existing, never re-minted (skip-only).
    ac = result["sections"]["access_control"]
    assert ac["created"] == 0, ac
    assert ac["updated"] == 0, ac
    assert ac["details"]["new_api_keys"] == [], ac  # a token mint would surface here
    assert ac["details"]["skipped_existing"] >= 1, ac
    assert ac["errors"] == [], ac


# A deployment X-band key: the readiness sentinel path the launcher reads from the process env.
_X_BAND_KEY = "TAI_READY_SENTINEL_PATH"


@pytest.mark.needs("process")
async def test_backup_import_env_x_band_key_is_refused(fresh_stack: Callable[..., TaiStack]) -> None:
    """A crafted backup whose env section carries a deployment X-band key
    (``TAI_READY_SENTINEL_PATH``) is REFUSED loudly: backup env restore rides
    ``apply_env_change`` (``backup/sections.py``), so the shared boundary validator fires
    there with no backup-specific code. The section report carries the error naming the
    offending key, and nothing lands — the X-band value never reaches the store, nor does the
    sibling key in the same (atomically refused) section (validate-before-write)."""
    target = fresh_stack(build_backup_source_stack)
    api = target.api()
    before = (await api.get("/api/config/env"))["env"]
    assert _X_BAND_KEY not in before

    document = {
        "version": 1,
        "sections": {"env": {"E2E_BACKUP_OK": "harmless", _X_BAND_KEY: "spoofed-sentinel"}},
        "errors": {},
    }
    result = await api.post("/api/backup/import", json={"document": document, "sections": ["env"]})
    assert result["ok"] is False, f"an X-band backup env import must not report ok: {result}"
    errors = result["sections"]["env"]["errors"]
    assert errors, f"the env section report carried no error: {result}"
    assert any(_X_BAND_KEY in str(err) for err in errors), f"the refusal did not name the X-band key: {errors}"

    # Nothing landed — the whole section was refused before any write.
    after = (await api.get("/api/config/env"))["env"]
    assert _X_BAND_KEY not in after, f"the X-band value leaked into the stored env: {after}"
    assert after.get("E2E_BACKUP_OK") != "harmless", f"a refused section still wrote a sibling key: {after}"


@pytest.mark.needs("process", "files")
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
    result = await api.post("/api/backup/import", json={"document": corrupt, "sections": ["manifest"]})
    assert result["ok"] is False, f"a corrupted manifest import must not report ok: {result}"
    assert result["sections"]["manifest"]["errors"], f"the section report carried no error: {result}"
    assert manifest_file(target).read_bytes() == before, "a failed manifest import must not have touched the manifest"


@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "kind:states",
    "setting:two-stacks",
    "setting:conversations:redis",
)
async def test_stored_definitions_restore_their_bindings_onto_a_fresh_stack(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """One backup carries a state, a hook and a per-target conversation config that bind it,
    and a preset the config targets; restored onto a FRESH stack in one import:

    * ``states`` replays before ``webhooks``, so the hook whose binding names the restored
      state restores, and its template is attached on the fresh stack by the hook restore
      itself (the backup carries the state and the template but not the attachment);
    * the config restores although its target preset does not exist yet when the
      ``conversation_target_config`` section replays (presets restore later, in
      ``versioned_documents``): target existence is the set door's rule, never the restore's;
    * that door still refuses the same body on the fresh stack, whose live registry has not
      loaded the restored preset.
    """
    source = fresh_stack(build_backup_definitions_stack)
    api = source.api()
    state = uniq("bkpstatus")
    template = uniq("bkp-tpl").replace("_", "-")
    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e backup binding target",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api.put(
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
        },
    )
    binding = {"states": [{"state": state, "subject_expr": {"content": ".x"}, "templates": [template]}]}
    hook_name = uniq("bkp-hook").replace("_", "-")
    await api.post(
        "/api/hooks",
        json={
            "name": hook_name,
            "topic": uniq("bkp-topic").replace("_", "-"),
            "tool": "e2e_echo",
            "execution_key": uniq("bkp-exec"),
            "state_binding": binding,
        },
    )
    preset = uniq("bkp-preset").replace("_", "-")
    await api.post(
        "/api/presets",
        json={"name": preset, "base_tool": "e2e_echo", "description": "backup target", "fixed_kwargs": {}},
    )
    config_body = {"multichannel": False, "greeting_template": None, "state_binding": binding}
    await api.put(f"/api/conversation-configs/tool/{preset}", json=config_body)
    sections = ["states", "webhooks", "conversation_target_config", "versioned_documents"]
    export = await api.request_raw("POST", "/api/backup/export", json={"sections": sections})
    assert export.status_code == 200, export.text
    document = export.json()
    # The source attached the template when the hook was registered; drop that attachment from
    # the backup so the attachment the fresh stack ends with is the one the restore itself makes.
    states_payload = document["sections"]["states"]
    assert any(row["state"] == state for row in states_payload["attachments"]), states_payload
    states_payload["attachments"] = [row for row in states_payload["attachments"] if row["state"] != state]

    target = fresh_stack(build_backup_definitions_stack)
    fresh = target.api()
    result = await fresh.post("/api/backup/import", json={"document": document, "sections": sections})
    assert result["ok"], result
    assert result["sections"]["webhooks"]["created"] == 1, result
    assert result["sections"]["conversation_target_config"]["created"] == 1, result
    assert all(result["sections"][name]["errors"] == [] for name in sections), result

    served = await fresh.get(f"/api/states/{state}")
    assert [m["template"] for m in served["attachments"]] == [template], served
    hooks = await fresh.get("/api/hooks")
    assert hook_name in {item["name"] for item in hooks["items"]}, hooks
    config = await fresh.get(f"/api/conversation-configs/tool/{preset}")
    restored_attach = config["state_binding"]["states"][0]
    assert (restored_attach["state"], restored_attach["templates"]) == (state, [template]), config

    refused = await fresh.request_raw("PUT", f"/api/conversation-configs/tool/{preset}", json=config_body)
    assert refused.status_code == 400, refused.text
    expected = f"conversation config target tool {preset!r} is not a registered tool"
    assert refused.json()["error"] == expected, refused.text


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.needs("kind:storage")
async def test_storage_section_restores_text_and_binary_objects_byte_equal(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """The ``storage`` section carries every object of the content store: a text object as
    ``content_text`` and a binary one as ``content_base64``, each with its content type. Both
    are exported, deleted, restored, and then download byte-equal."""
    stack = fresh_stack(build_backup_storage_stack)
    api = stack.api()
    text_id = f"{uniq('bkp-text')}.j2"
    text = f"hello {{{{ name }}}} {uniq('body')}"
    binary_id = f"{uniq('bkp-blob')}.png"
    raw = _PNG_SIGNATURE + secrets.token_bytes(48)
    await api.post("/api/storage/resources", json={"id": text_id, "content_text": text})
    await api.post("/api/storage/resources", json={"id": binary_id, "content_base64": base64.b64encode(raw).decode()})
    binary_type = (await api.get(f"/api/storage/resources/{binary_id}/stat"))["content_type"]

    export = await api.request_raw("POST", "/api/backup/export", json={"sections": ["storage"]})
    assert export.status_code == 200, export.text
    document = export.json()
    assert document["errors"] == {}, document["errors"]
    entries = document["sections"]["storage"]
    assert entries[text_id]["content_text"] == text, entries[text_id]
    assert base64.b64decode(entries[binary_id]["content_base64"]) == raw
    assert entries[binary_id]["content_type"] == binary_type, entries[binary_id]

    for resource_id in (text_id, binary_id):
        await api.delete(f"/api/storage/resources/{resource_id}")

    result = await api.post("/api/backup/import", json={"document": document, "sections": ["storage"]})
    assert result["ok"], result
    report = result["sections"]["storage"]
    assert report["created"] == 2, report
    assert report["errors"] == [], report

    for resource_id, expected in ((text_id, text.encode()), (binary_id, raw)):
        download = await api.request_raw("GET", f"/api/storage/resources/{resource_id}/content")
        assert download.status_code == 200, download.text
        assert download.content == expected, resource_id
    restored_type = (await api.get(f"/api/storage/resources/{binary_id}/stat"))["content_type"]
    assert restored_type == binary_type


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


@pytest.mark.needs("kind:storage")
async def test_a_storage_import_refuses_rows_under_the_inbound_media_prefix_and_changes_nothing(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """A hand-made archive naming the not-backed-up ``inbound-media/`` prefix, directly and
    through a non-canonical ``./`` key, has both rows refused and writes nothing."""
    stack = fresh_stack(build_backup_storage_stack)
    api = stack.api()
    media_id = uniq("media")
    marked = f"inbound-media/{media_id}"
    dotted = f"./inbound-media/{media_id}"
    before = (await api.get("/api/storage/resources"))["resources"]
    document = {
        "version": 1,
        "sections": {
            "storage": {
                marked: {"content_base64": base64.b64encode(_PNG_SIGNATURE).decode(), "content_type": "image/png"},
                dotted: {"content_text": "x", "content_type": None},
            }
        },
    }

    result = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["storage"], "mode": "overwrite"},
    )
    report = result["sections"]["storage"]
    assert report["errors"] == [
        f"template {marked!r}: path is under 'inbound-media/', which is marked not backed up",
        f"template {dotted!r}: {_non_canonical(dotted)}",
    ], report
    assert (report["created"], report["skipped"]) == (0, 2), report
    assert (await api.get("/api/storage/resources"))["resources"] == before
    await api.get(f"/api/storage/entries/{marked}", expect=404)
