"""C7 (P1) — a resource stored on replica A through the REAL active storage plugin
loads on replica B (a worker that never saw the write), and the plugin really
touched the filesystem in its OWN on-disk layout.

Storage-variant spec, not a local-storage spec: the on-disk read-back goes through
the selected storage variant's helper (``stored_object_path`` / ``read_stored``),
so it runs for both the local backend and the fixture backend (whose distinct
layout would defeat a hard-coded local-path assertion)."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack


async def test_resource_stored_on_a_loads_on_b_through_real_storage(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    storage = replicas_stack.infra.variants.storage
    root = replicas_stack.resources.storage_root
    rel_path = f"{uniq('tmpl')}.txt"
    content = f"hello {uniq('body')}"
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    await api_a.post("/api/upload-template", json={"path": rel_path, "content": content})

    # The plugin really wrote the filesystem under the stack's storage root, in the
    # variant's own layout — and the stored bytes decode back to the content.
    on_disk = storage.stored_object_path(root, rel_path)
    assert on_disk.exists(), f"storage plugin did not write {on_disk}"
    assert storage.read_stored(root, rel_path) == content, "stored bytes did not decode to the uploaded content"

    # B lists + loads the resource through the plugin.
    listing = await api_b.get("/api/templates")
    assert any(rel_path in str(item) for item in listing), f"B did not list {rel_path}: {listing}"
    loaded = await api_b.post("/api/template", json={"template_id": rel_path})
    assert content in str(loaded), f"B did not load the stored content: {loaded}"

    # Delete on B removes it from disk and from A's listing.
    await api_b.post("/api/delete-template", json={"path": rel_path})
    assert not on_disk.exists(), "delete did not remove the file from disk"
    listing_a = await api_a.get("/api/templates")
    assert not any(rel_path in str(item) for item in listing_a), "A still lists the deleted resource"
