"""The template-directory delete door (``POST /api/delete-template-dir``).

Unlike the idempotent single-template delete (an absent key is a no-op 200), a directory
that matches nothing is a loud 404; and a key spelling the template ROOT — which would
wipe the whole store — is refused by the key guard with a 400, ahead of the provider. Runs over the
real active storage provider (``core_stack``).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:storage")


async def test_delete_template_dir_absent_is_404(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    absent = uniq("no-such-dir")
    resp = await api.request_raw("POST", "/api/delete-template-dir", json={"path": absent})
    assert resp.status_code == 404, resp.text
    assert absent in resp.json()["error"], resp.text


@pytest.mark.parametrize("root", [".", "a/.."])
async def test_delete_template_dir_root_is_400(core_stack: TaiStack, root: str) -> None:
    api = core_stack.api()
    resp = await api.request_raw("POST", "/api/delete-template-dir", json={"path": root})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == (
        f"non-canonical store key {root!r}: a key is '/'-separated names with no empty, '.' or '..' segment"
    )
