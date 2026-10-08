"""The access-control fences on a deployment served under a path prefix, e2e.

The embed host mounts the tai app at ``/mnt``, so every request reaches it with
``root_path="/mnt"`` and a request path that still carries the prefix. Every
access-control decision reads the canonical path with the mount prefix removed, so the
seeded editor role's jq ceiling — written against ``/api/auth/...`` — fences the
control-plane writes exactly as it does on a root deployment, and the editor's own
reads stay open.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tai42_e2e.seeding import seed_editor_key
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "topology:embed",
    "setting:embed-host-prefix",
    "kind:identity",
    "store:postgres",
    "store:redis",
    "setting:seeded-access-control",
)


@pytest.fixture(scope="module")
def editor_client(embed_prefix_stack: TaiStack) -> Iterator[httpx.Client]:
    """An HTTP client on the prefixed app authenticating with a key whose owner holds the editor role."""
    stack = embed_prefix_stack
    token = seed_editor_key(stack.infra, stack.resources, owner_id="prefix-editor", key_id="prefix-editor-key")
    with httpx.Client(base_url=stack.base_url(), headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as client:
        yield client


def test_the_app_is_served_under_the_prefix(embed_prefix_stack: TaiStack) -> None:
    assert embed_prefix_stack.base_url().endswith("/mnt")
    assert httpx.get(f"{embed_prefix_stack.base_url()}/health", timeout=10.0).status_code == 200


def test_editor_reads_its_own_identity_under_the_prefix(editor_client: httpx.Client) -> None:
    response = editor_client.get("/api/auth/me")
    assert response.status_code == 200, response.text


def test_editor_cannot_pin_a_public_route_under_the_prefix(editor_client: httpx.Client) -> None:
    # The control-plane write is outside the editor ceiling; the prefix does not hide it.
    response = editor_client.post("/api/auth/public-routes", json={"url": "/api/prefix-probe"})
    assert response.status_code == 403, response.text


def test_unauthenticated_request_under_the_prefix_is_refused(embed_prefix_stack: TaiStack) -> None:
    response = httpx.get(f"{embed_prefix_stack.base_url()}/api/auth/me", timeout=10.0)
    assert response.status_code == 401, response.text
