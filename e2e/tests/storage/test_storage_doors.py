"""The ``/api/storage*`` HTTP surface and the templates entry door over the REAL active
storage provider (round-trip, base64 binary, the text/document/binary entries, directory
delete, the input-guard 400s, the content refusals) and the honest absent-provider answers
(``present: false`` + 501) on a stack that mounts the door but loads no provider.

The identity door's expected answer is variant-owned, so the suite drives whichever
storage plugin the storage axis selected."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.content_samples import (
    JAVASCRIPT,
    NOTES_PDF,
    RAGGED_CSV,
    SQL,
    SQL_LATIN1,
    locked_pdf,
    locked_pdf_message,
    not_loadable_prefix,
    png,
    ragged_csv_message,
)
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

_STORAGE_ENTRY_KEYS = {"kind", "id", "content_type", "size", "content"}


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


async def _upload_text(api: ApiClient, resource_id: str, content: str) -> None:
    stored = await api.post("/api/storage/resources", json={"id": resource_id, "content_text": content})
    assert stored == {"id": resource_id, "stored": True}


async def _upload_bytes(api: ApiClient, resource_id: str, data: bytes) -> None:
    stored = await api.post(
        "/api/storage/resources", json={"id": resource_id, "content_base64": base64.b64encode(data).decode()}
    )
    assert stored == {"id": resource_id, "stored": True}


async def _entries(api: ApiClient, resource_id: str, *, expect: int = 200) -> tuple[Any, Any]:
    """The object as the storage door and the templates door answer it."""
    storage = await api.get(f"/api/storage/entries/{resource_id}", expect=expect)
    template = await api.post("/api/template", json={"template_id": resource_id}, expect=expect)
    return storage, template


@pytest.mark.needs("kind:storage", "setting:storage-variant")
async def test_storage_identity_and_text_round_trip(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    storage = core_stack.infra.variants.storage
    info = await api.get("/api/storage")
    assert info == {
        "present": True,
        "provider": storage.provider_class,
        "module": storage.provider_module,
        "surfaces": ["storage", "templates"],
    }

    resource_id = f"{uniq('notes')}.txt"
    content = f"hello {uniq('body')}"
    await _upload_text(api, resource_id, content)

    listing = await api.get("/api/storage/resources")
    assert resource_id in listing["resources"]

    stat = await api.get(f"/api/storage/resources/{resource_id}/stat")
    assert stat["id"] == resource_id
    assert "content_type" in stat

    download = await api.request_raw("GET", f"/api/storage/resources/{resource_id}/content")
    assert download.status_code == 200, download.text
    assert download.content == content.encode()
    assert "attachment" in download.headers.get("content-disposition", "")

    deleted = await api.delete(f"/api/storage/resources/{resource_id}")
    assert deleted == {"id": resource_id, "deleted": True}

    listing_after = await api.get("/api/storage/resources")
    assert resource_id not in listing_after["resources"]


@pytest.mark.needs("kind:storage")
async def test_storage_base64_binary_round_trip(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    resource_id = f"{uniq('blob')}.bin"
    raw = secrets.token_bytes(48)
    await _upload_bytes(api, resource_id, raw)

    download = await api.request_raw("GET", f"/api/storage/resources/{resource_id}/content")
    assert download.status_code == 200, download.text
    assert download.content == raw

    await api.delete(f"/api/storage/resources/{resource_id}")


@pytest.mark.needs("kind:storage")
async def test_both_entry_doors_answer_text_and_binary_with_the_uploaded_size(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = core_stack.api()
    text_id = f"{uniq('notes')}/greeting.txt"
    content = f"Hello {{{{ name }}}} {uniq('body')}"
    await _upload_text(api, text_id, content)
    image_id = f"{uniq('images')}/dot.png"
    image = png()
    await _upload_bytes(api, image_id, image)

    stored_text, template_text = await _entries(api, text_id)
    assert set(stored_text) == _STORAGE_ENTRY_KEYS
    assert (stored_text["kind"], stored_text["id"], stored_text["content"]) == ("text", text_id, content)
    assert stored_text["size"] == len(content.encode())
    assert (template_text["kind"], template_text["content"], template_text["size"]) == (
        "text",
        content,
        len(content.encode()),
    )
    assert isinstance(template_text["schema"], dict)
    assert list(template_text["schema"]["properties"]) == ["name"]
    assert template_text["schema_error"] is None

    stored_image, template_image = await _entries(api, image_id)
    assert set(stored_image) == {"kind", "id", "content_type", "size"}
    assert (stored_image["kind"], stored_image["size"]) == ("binary", len(image))
    assert (template_image["kind"], template_image["size"], template_image["schema"]) == ("binary", len(image), None)


@pytest.mark.needs("kind:storage")
async def test_broken_jinja_opens_on_both_doors_with_the_schema_error_on_the_templates_door(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = core_stack.api()
    resource_id = f"{uniq('broken')}.txt"
    content = "Hello {{ name }"
    await _upload_text(api, resource_id, content)

    stored, template = await _entries(api, resource_id)
    assert set(stored) == _STORAGE_ENTRY_KEYS
    assert (stored["kind"], stored["content"]) == ("text", content)
    assert (template["kind"], template["content"], template["schema"], template["schema_error"]) == (
        "text",
        content,
        None,
        "template error: unexpected '}'",
    )


@pytest.mark.needs("kind:storage")
async def test_a_missing_object_stat_is_404(core_stack: TaiStack) -> None:
    missing = await core_stack.api().get("/api/storage/resources/missing.txt/stat", expect=404)
    assert missing == {"error": "resource 'missing.txt' not found"}


@pytest.mark.needs("kind:storage")
async def test_a_pdf_is_a_document_on_both_entry_doors(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    resource_id = f"{uniq('docs')}/notes.pdf"
    await _upload_bytes(api, resource_id, NOTES_PDF)

    stored, template = await _entries(api, resource_id)
    for entry in (stored, template):
        assert (entry["kind"], entry["size"]) == ("document", len(NOTES_PDF))
        assert "Quarterly notes" in entry["content"]
    assert template["schema"] is None


@pytest.mark.needs("kind:storage")
async def test_sql_javascript_and_the_empty_object_are_text_and_the_sql_renders(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = core_stack.api()
    sql_id = f"{uniq('queries')}/lookup.sql"
    script_id = f"{uniq('app')}/greet.js"
    empty_id = f"{uniq('empty')}.txt"
    await _upload_text(api, sql_id, SQL)
    await _upload_text(api, script_id, JAVASCRIPT)
    await _upload_text(api, empty_id, "")

    for resource_id, content in ((sql_id, SQL), (script_id, JAVASCRIPT), (empty_id, "")):
        stored, template = await _entries(api, resource_id)
        assert (stored["kind"], stored["content"]) == ("text", content)
        assert (template["kind"], template["content"]) == ("text", content)
        if resource_id == sql_id:
            assert list(template["schema"]["properties"]) == ["account_id"]

    rendered = await api.post("/api/render-template", json={"text": {"id": sql_id, "kwargs": {"account_id": 7}}})
    assert rendered == {"rendered": "SELECT id, name FROM accounts WHERE id = 7 ORDER BY name LIMIT 10;"}


@pytest.mark.needs("kind:storage")
async def test_content_an_entry_door_refuses_is_400_on_both_doors(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = core_stack.api()
    latin_id = f"{uniq('queries')}/latin.sql"
    locked_id = f"{uniq('docs')}/locked.pdf"
    ragged_id = f"{uniq('data')}/scores.csv"
    await _upload_bytes(api, latin_id, SQL_LATIN1)
    await _upload_bytes(api, locked_id, locked_pdf())
    await _upload_text(api, ragged_id, RAGGED_CSV)

    for refusal in await _entries(api, latin_id, expect=400):
        assert refusal["error"].startswith(not_loadable_prefix(latin_id))
        assert "is detected as text but is not UTF-8" in refusal["error"]
    locked = {"error": not_loadable_prefix(locked_id) + locked_pdf_message(locked_id)}
    assert await _entries(api, locked_id, expect=400) == (locked, locked)
    ragged = {"error": not_loadable_prefix(ragged_id) + ragged_csv_message(ragged_id)}
    assert await _entries(api, ragged_id, expect=400) == (ragged, ragged)


@pytest.mark.needs("kind:storage")
async def test_storage_delete_dir_removes_nested_set(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    directory = uniq("reports")
    ids = [f"{directory}/2026/{uniq('a')}.txt", f"{directory}/2026/{uniq('b')}.txt", f"{directory}/{uniq('c')}.txt"]
    for resource_id in ids:
        await api.post("/api/storage/resources", json={"id": resource_id, "content_text": "x"})

    listing = await api.get("/api/storage/resources")
    assert all(resource_id in listing["resources"] for resource_id in ids)

    deleted = await api.delete(f"/api/storage/dirs/{directory}")
    assert deleted == {"dir": directory, "deleted": True}

    listing_after = await api.get("/api/storage/resources")
    assert all(resource_id not in listing_after["resources"] for resource_id in ids)


@pytest.mark.needs("kind:storage")
async def test_storage_upload_one_of_is_enforced(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    resource_id = f"{uniq('bad')}.txt"

    both = await api.request_raw(
        "POST",
        "/api/storage/resources",
        json={"id": resource_id, "content_text": "t", "content_base64": base64.b64encode(b"t").decode()},
    )
    assert both.status_code == 400, both.text
    assert "exactly one" in both.json()["error"]

    neither = await api.request_raw("POST", "/api/storage/resources", json={"id": resource_id})
    assert neither.status_code == 400, neither.text
    assert "exactly one" in neither.json()["error"]


@pytest.mark.needs("kind:storage")
async def test_a_non_canonical_key_is_refused_on_the_http_and_mcp_doors(core_stack: TaiStack) -> None:
    api = core_stack.api()
    before = (await api.get("/api/storage/resources"))["resources"]

    for key in ("a//b", "../escape.txt"):
        refused = await api.post("/api/storage/resources", json={"id": key, "content_text": "x"}, expect=400)
        assert refused == {"error": _non_canonical(key)}
    assert (await api.get("/api/storage/resources"))["resources"] == before

    async with core_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "upload_resource", {"resource_id": "./x", "content_text": "x"}, raise_on_error=False
        )
    assert result.is_error
    assert "".join(getattr(block, "text", "") for block in result.content) == _non_canonical("./x")

    # A percent-encoded ``..`` survives client-side URL normalisation; it is refused either by
    # the key guard or by a path-normalising gateway in front of the app, so no file is addressed.
    read = await api.request_raw("GET", "/api/storage/resources/%2e%2e/secret/stat")
    assert read.status_code in (400, 404), read.text
    if read.status_code == 400:
        assert read.json() == {"error": _non_canonical("../secret")}


@pytest.mark.needs("setting:default-routers-all")
async def test_the_kinds_door_reports_file_loading_active(default_router_stack: TaiStack) -> None:
    rows = await default_router_stack.api().get("/api/system/kinds")
    assert [row for row in rows if row["kind"] == "files"] == [
        {"kind": "files", "state": "active", "plugin": None, "detail": "file loading is installed (the 'files' extra)"}
    ]


@pytest.mark.needs("setting:storage_module=absent")
async def test_storage_absent_provider_is_honest(bare_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = bare_stack.api()

    info = await api.get("/api/storage")
    assert info == {"present": False, "provider": None, "module": None, "surfaces": ["storage", "templates"]}

    resources = await api.request_raw("GET", "/api/storage/resources")
    assert resources.status_code == 501, resources.text
    assert "storage-provider plugin" in resources.json()["error"]

    upload = await api.request_raw(
        "POST", "/api/storage/resources", json={"id": f"{uniq('x')}.txt", "content_text": "x"}
    )
    assert upload.status_code == 501, upload.text
    assert "storage-provider plugin" in upload.json()["error"]
