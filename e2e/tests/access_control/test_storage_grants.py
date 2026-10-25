"""The storage and templates grants' reach over the presence, list and entry doors.

A role granting one level of one tag reaches exactly its row: presence answers every
storage or templates grant; the storage list and entry read answer ``storage`` at
read or write; the templates list answers ``templates`` at read or write; the
templates entry door is a write-classed POST, so only ``templates: write`` opens it.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

import pytest

from tai42_e2e.stack import TaiStack

from ._rbac_support import create_role, create_user_with_role

pytestmark = pytest.mark.needs(
    "kind:identity",
    "kind:accounts:postgres",
    "kind:storage",
    "topology:replicas",
    "setting:seeded-access-control",
)


def _doors(resource_id: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("GET", "/api/storage", None),
        ("GET", "/api/storage/resources", None),
        ("GET", f"/api/storage/entries/{quote(resource_id)}", None),
        ("GET", "/api/templates", None),
        ("POST", "/api/template", {"template_id": resource_id}),
    ]


# Per grant: whether each door of ``_doors`` answers 200 (else 403), in order.
_REACH = {
    ("storage", "read"): [True, True, True, False, False],
    ("storage", "write"): [True, True, True, False, False],
    ("templates", "read"): [True, False, False, True, False],
    ("templates", "write"): [True, False, False, True, True],
}


@pytest.mark.parametrize(("tag", "level"), list(_REACH))
async def test_a_storage_or_templates_grant_reaches_its_doors(
    accounts_stack: TaiStack, uniq: Callable[[str], str], tag: str, level: str
) -> None:
    admin = accounts_stack.api(port=accounts_stack.port_a)
    resource_id = f"{uniq('grants')}/note.txt"
    await admin.post("/api/storage/resources", json={"id": resource_id, "content_text": "Hello {{ name }}"})
    try:
        role = uniq(f"{tag}-{level}")
        await create_role(admin, name=role, base_tier="editor", grants={tag: level})
        _, session_token = await create_user_with_role(accounts_stack, admin, uniq, role=role)
        holder = accounts_stack.api(port=accounts_stack.port_b).with_token(session_token)
        reached = []
        for method, path, body in _doors(resource_id):
            response = await holder.request_raw(method, path, json=body)
            assert response.status_code in (200, 403), f"{method} {path}: {response.status_code} {response.text}"
            reached.append(response.status_code == 200)
        assert reached == _REACH[(tag, level)]
    finally:
        await admin.delete(f"/api/storage/resources/{resource_id}")
