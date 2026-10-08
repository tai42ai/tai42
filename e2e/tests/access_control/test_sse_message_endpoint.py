"""The SSE transport's message endpoint behind access control, e2e.

``tai serve --transport sse`` serves the event stream at ``/sse`` and mounts the message
endpoint at ``/messages``; a client posts to ``/messages?session_id=...`` (or
``/messages/?session_id=...``). Both spellings reduce to the canonical ``/messages``, a
recorded credential-gated surface: an unauthenticated post is refused 401, and an
authenticated editor passes the gate and reaches the transport (which answers for the
unknown session itself).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tai42_e2e.seeding import seed_editor_key
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "setting:transport:sse",
    "kind:identity",
    "store:postgres",
    "store:redis",
    "setting:seeded-access-control",
)

_MESSAGE_PATHS = ["/messages?session_id=x", "/messages/?session_id=x"]


@pytest.fixture(scope="module")
def editor_token(sse_auth_stack: TaiStack) -> str:
    stack = sse_auth_stack
    return seed_editor_key(stack.infra, stack.resources, owner_id="sse-editor", key_id="sse-editor-key")


@pytest.fixture
def client(sse_auth_stack: TaiStack) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=sse_auth_stack.base_url(), timeout=30.0) as http:
        yield http


@pytest.mark.parametrize("path", _MESSAGE_PATHS)
def test_unauthenticated_post_to_the_message_endpoint_is_refused_401(client: httpx.Client, path: str) -> None:
    response = client.post(path, json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("path", _MESSAGE_PATHS)
def test_an_editor_post_to_the_message_endpoint_passes_the_gate(
    client: httpx.Client, editor_token: str, path: str
) -> None:
    response = client.post(
        path,
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Authorization": f"Bearer {editor_token}"},
    )
    assert response.status_code not in (401, 403), response.text
