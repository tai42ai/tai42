"""The template-directory delete door (``POST /api/delete-template-dir``).

Unlike the idempotent single-template delete (an absent key is a no-op 200), a directory
that matches nothing is a loud 404; and a key resolving to the template ROOT — which would
wipe the whole store — is refused with a 400 HERE, ahead of the provider. Runs over the
real active storage provider (``core_stack``).
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack


async def test_delete_template_dir_absent_is_404(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    absent = uniq("no-such-dir")
    resp = await api.request_raw("POST", "/api/delete-template-dir", json={"path": absent})
    assert resp.status_code == 404, resp.text
    assert absent in resp.json()["error"], resp.text


async def test_delete_template_dir_root_is_400(core_stack: TaiStack) -> None:
    api = core_stack.api()
    # ``.`` resolves to the template ROOT; the lexical guard admits a root-resolving key,
    # so the door's own destructive-delete check is what must refuse it.
    resp = await api.request_raw("POST", "/api/delete-template-dir", json={"path": "."})
    assert resp.status_code == 400, resp.text
    assert "root" in resp.json()["error"].lower(), resp.text
