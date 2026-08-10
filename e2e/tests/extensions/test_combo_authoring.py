"""Granular extension-combo ops (M36 E): add/remove a SINGLE combo on a tool through
``POST /api/tools/{name}/extensions/combos``, instead of the whole-list replace ``POST
/api/tools/{name}/extensions`` forces.

The ``e2e_record`` probe carries exactly one authored combo (``[["cache"]]``). A second
combo is added, a GET verifies both, the first is removed, a GET verifies the shrink;
then adding a combo already present is a loud 400 naming it and removing one that is
absent a loud 404 — the mission's loud-membership rule on the combo surface."""

from __future__ import annotations

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

_TOOL = "e2e_record"


async def _combos(stack: TaiStack) -> list[list[str]]:
    """The tool's full list-of-combos (the existing GET shape ``POST .../combos`` also
    returns) — the read-back operand after each granular mutation."""
    body = await stack.api().get(f"/api/tools/{_TOOL}/extensions", retry_on_reloading=True)
    return body["combos"]


async def test_extension_combos_add_remove(extensions_stack: TaiStack) -> None:
    api = extensions_stack.api()
    combos_path = f"/api/tools/{_TOOL}/extensions/combos"

    # The probe seeds exactly one authored combo.
    assert await _combos(extensions_stack) == [["cache"]]

    # Add a second combo: it appends, leaving the first intact.
    await api.post(combos_path, json={"add": [["monitor"]]}, retry_on_reloading=True)
    assert await _combos(extensions_stack) == [["cache"], ["monitor"]]

    # Remove the first combo: only it drops.
    await api.post(combos_path, json={"remove": [["cache"]]}, retry_on_reloading=True)
    assert await _combos(extensions_stack) == [["monitor"]]

    # Adding a combo already present is a loud 400 naming it (never a silent no-op).
    dup = await api.post(combos_path, json={"add": [["monitor"]]}, expect=400, retry_on_reloading=True)
    assert "monitor" in dup["error"], dup

    # Removing a combo the tool no longer carries is a loud 404 naming it.
    missing = await api.post(combos_path, json={"remove": [["cache"]]}, expect=404, retry_on_reloading=True)
    assert "cache" in missing["error"], missing
