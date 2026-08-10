"""B6 — the resources router's read behavior and its envelope contract.

Beyond the mount-anchor coverage in ``tests/routing/test_default_router_coverage.py``:
store a resource through the storage surface, then ``POST /api/resources/get``
returns it under the success envelope ``{"data": ...}``, while an unknown resource
is a ``404`` under the failure envelope ``{"error": ...}`` (the contract stated in
``routers/resources.py``). The envelope KEYS are the assertion — a success
carries ``data`` and no ``error``, a miss carries ``error`` and no ``data`` — so a
regression that leaked the wrong shape (or a 200 on a miss) fails here."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack


async def test_stored_resource_reads_back_and_unknown_is_404(
    resources_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = resources_stack.api()
    resource_id = f"{uniq('probe')}.txt"
    content = f"resource-body-{uniq('marker')}"

    # Store the resource through the real storage provider's CRUD door.
    await api.post("/api/storage/resources", json={"id": resource_id, "content_text": content})

    # POST /api/resources/get with no template_kwargs returns the content as-is under
    # the success envelope. Assert the raw envelope shape, not just the unwrapped value.
    ok = await api.request_raw("POST", "/api/resources/get", json={"resource_id": resource_id})
    assert ok.status_code == 200, ok.text
    ok_body = ok.json()
    assert isinstance(ok_body, dict), ok_body
    assert "data" in ok_body, ok_body
    assert "error" not in ok_body, ok_body
    assert content in str(ok_body["data"]), ok_body

    # An unknown resource is a 404 carrying the failure envelope, never a success shape.
    missing = await api.request_raw("POST", "/api/resources/get", json={"resource_id": f"{uniq('nope')}.txt"})
    assert missing.status_code == 404, missing.text
    miss_body = missing.json()
    assert isinstance(miss_body, dict), miss_body
    assert "error" in miss_body, miss_body
    assert "data" not in miss_body, miss_body
