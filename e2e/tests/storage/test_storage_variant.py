"""A resource stored on replica A through the REAL active storage plugin
loads on replica B (a worker that never saw the write), and the plugin really wrote
its OWN store.

Storage-variant spec, not a local-storage spec: the read-back goes through the
selected storage variant's store-agnostic seam (``assert_stored`` / ``assert_absent``),
so it runs for every storage backend — the filesystem locals, the S3 bucket, the
fake GitHub repo — each asserting against its own store through an independent client."""

from __future__ import annotations

import base64
from collections.abc import Callable

import pytest

from tai42_e2e.content_samples import png
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:storage", "topology:replicas", "files")


async def test_resource_stored_on_a_loads_on_b_through_real_storage(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    storage = replicas_stack.infra.variants.storage
    res = replicas_stack.resources
    rel_path = f"{uniq('tmpl')}.txt"
    content = f"hello {uniq('body')}"
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    await api_a.post("/api/upload-template", json={"path": rel_path, "content": content})

    # The plugin really wrote its own store, and the stored bytes decode back to the
    # content — asserted through the variant's independent client, not the plugin.
    storage.assert_stored(res, rel_path, content)

    # B lists + loads the resource through the plugin.
    listing = await api_b.get("/api/templates")
    assert any(rel_path in str(item) for item in listing), f"B did not list {rel_path}: {listing}"
    loaded = await api_b.post("/api/template", json={"template_id": rel_path})
    assert loaded["kind"] == "text"
    assert loaded["content"] == content

    # Delete on B removes it from the store and from A's listing.
    await api_b.post("/api/delete-template", json={"path": rel_path})
    storage.assert_absent(res, rel_path)
    listing_a = await api_a.get("/api/templates")
    assert not any(rel_path in str(item) for item in listing_a), "A still lists the deleted resource"


async def test_an_overwrite_on_a_reads_fresh_on_b_through_the_entry_door(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    resource_id = f"{uniq('notes')}/current.txt"
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    first = f"first {uniq('body')}"
    second = f"second, and longer: {uniq('body')}"
    image = png(8, 8)

    await api_a.post("/api/storage/resources", json={"id": resource_id, "content_text": first})
    entry = await api_b.get(f"/api/storage/entries/{resource_id}")
    assert (entry["kind"], entry["content"], entry["size"]) == ("text", first, len(first.encode()))

    await api_a.post("/api/storage/resources", json={"id": resource_id, "content_text": second})
    entry = await api_b.get(f"/api/storage/entries/{resource_id}")
    assert (entry["kind"], entry["content"], entry["size"]) == ("text", second, len(second.encode()))

    # The binary entry is classified from ranged reads, so the kind proves those read the new bytes.
    await api_a.post(
        "/api/storage/resources", json={"id": resource_id, "content_base64": base64.b64encode(image).decode()}
    )
    entry = await api_b.get(f"/api/storage/entries/{resource_id}")
    assert (entry["kind"], entry["size"]) == ("binary", len(image))
    download = await api_b.request_raw("GET", f"/api/storage/resources/{resource_id}/content")
    assert download.status_code == 200, download.text
    assert download.content == image

    await api_a.delete(f"/api/storage/resources/{resource_id}")
