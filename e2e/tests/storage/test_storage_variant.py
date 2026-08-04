"""C7 (P1) — a resource stored on replica A through the REAL active storage plugin
loads on replica B (a worker that never saw the write), and the plugin really wrote
its OWN store.

Storage-variant spec, not a local-storage spec: the read-back goes through the
selected storage variant's store-agnostic seam (``assert_stored`` / ``assert_absent``),
so it runs for every storage backend — the filesystem locals, the S3 bucket, the
fake GitHub repo — each asserting against its own store through an independent client."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack


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
    assert content in str(loaded), f"B did not load the stored content: {loaded}"

    # Delete on B removes it from the store and from A's listing.
    await api_b.post("/api/delete-template", json={"path": rel_path})
    storage.assert_absent(res, rel_path)
    listing_a = await api_a.get("/api/templates")
    assert not any(rel_path in str(item) for item in listing_a), "A still lists the deleted resource"
