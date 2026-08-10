"""C7 — the ``/api/storage*`` HTTP surface over the REAL active storage provider
(round-trip, base64 binary, directory delete, the input-guard 400s) and the honest
absent-provider answers (``present: false`` + 501) on a stack that mounts the door
but loads no provider.

The identity door's expected answer is variant-owned, so the suite drives whichever
storage plugin the storage axis selected."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable

from tai42_e2e.stack import TaiStack


async def test_storage_identity_and_text_round_trip(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    storage = core_stack.infra.variants.storage
    info = await api.get("/api/storage")
    assert info == {"present": True, "provider": storage.provider_class, "module": storage.provider_module}

    resource_id = f"{uniq('notes')}.txt"
    content = f"hello {uniq('body')}"

    stored = await api.post("/api/storage/resources", json={"id": resource_id, "content_text": content})
    assert stored == {"id": resource_id, "stored": True}

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


async def test_storage_base64_binary_round_trip(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    resource_id = f"{uniq('blob')}.bin"
    raw = secrets.token_bytes(48)

    stored = await api.post(
        "/api/storage/resources",
        json={"id": resource_id, "content_base64": base64.b64encode(raw).decode()},
    )
    assert stored == {"id": resource_id, "stored": True}

    download = await api.request_raw("GET", f"/api/storage/resources/{resource_id}/content")
    assert download.status_code == 200, download.text
    assert download.content == raw

    await api.delete(f"/api/storage/resources/{resource_id}")


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


async def test_storage_rejects_dotdot_on_read_and_upload_body(core_stack: TaiStack) -> None:
    api = core_stack.api()

    # Upload-body case: the ``..`` rides in the JSON ``id`` (never URL-normalised),
    # so the router's own guard is what rejects it.
    upload = await api.request_raw("POST", "/api/storage/resources", json={"id": "../escape.txt", "content_text": "x"})
    assert upload.status_code == 400, upload.text
    assert "relative path" in upload.json()["error"]

    # Read case: a percent-encoded ``..`` segment survives client-side URL
    # normalisation and reaches the door as a literal ``..``.
    read = await api.request_raw("GET", "/api/storage/resources/%2e%2e/secret/stat")
    assert read.status_code == 400, read.text
    assert "relative path" in read.json()["error"]


async def test_storage_absent_provider_is_honest(bare_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = bare_stack.api()

    info = await api.get("/api/storage")
    assert info == {"present": False, "provider": None, "module": None}

    resources = await api.request_raw("GET", "/api/storage/resources")
    assert resources.status_code == 501, resources.text
    assert "storage-provider plugin" in resources.json()["error"]

    upload = await api.request_raw(
        "POST", "/api/storage/resources", json={"id": f"{uniq('x')}.txt", "content_text": "x"}
    )
    assert upload.status_code == 501, upload.text
    assert "storage-provider plugin" in upload.json()["error"]
