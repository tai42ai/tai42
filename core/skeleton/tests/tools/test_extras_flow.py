"""``extras`` on the start path: the door value reaches the started tool's frame and stops there.

``run_tool``/``dispatch_scope`` forward the ``extras`` seam keyword into the call frame; the started
tool reads it through ``app.tools.extras()``, and every NESTED dispatch reads an empty mapping so
nothing leaks down. ``extras`` is an in-process seam keyword alone — no request model carries it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from tai42_contract.tools import current_extras

from tai42_skeleton.tools.dispatch_scope import dispatch_scope

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP


def _fake_app() -> TaiMCP:
    return cast(
        "TaiMCP",
        SimpleNamespace(
            preset_manager=SimpleNamespace(is_registered=lambda _key: False, active_version=lambda _key: None)
        ),
    )


async def test_dispatch_scope_sets_extras_on_the_frame_and_a_nested_dispatch_reads_empty() -> None:
    app = _fake_app()
    async with dispatch_scope(app, "starter", extras={"seed": 1}):
        # The started tool sees the door's extras on its own frame.
        assert dict(current_extras()) == {"seed": 1}
        async with dispatch_scope(app, "nested"):
            # A nested dispatch starts empty — nothing leaks down.
            assert dict(current_extras()) == {}
        # Restored on exit of the nested frame.
        assert dict(current_extras()) == {"seed": 1}
    # And gone once the started tool's frame closes.
    assert dict(current_extras()) == {}


async def test_dispatch_scope_without_extras_reads_empty() -> None:
    async with dispatch_scope(_fake_app(), "plain"):
        assert dict(current_extras()) == {}
