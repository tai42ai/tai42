"""The write-time reference check: every tool name a write stores is looked up by that name."""

from __future__ import annotations

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.operations.errors import BadRequestError
from tai42_skeleton.operations.reference_check import RegistryReference, assert_references_resolve
from tai42_skeleton.tools.binding.facet import ToolBinding

from .presets.conftest import _manifest


@pytest.fixture
def no_full_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _listed(self: ToolBinding) -> dict:
        raise AssertionError("the reference check listed every tool to check a name")

    monkeypatch.setattr(ToolBinding, "get_tools", _listed)


async def test_a_registered_tool_resolves_by_its_name(no_full_listing: None) -> None:
    async with instance.app.app_context(_manifest()):
        await assert_references_resolve([RegistryReference("base tool", "weather"), RegistryReference("tool", "echo")])


async def test_an_unregistered_tool_is_refused_by_its_name(no_full_listing: None) -> None:
    async with instance.app.app_context(_manifest()):
        with pytest.raises(BadRequestError) as caught:
            await assert_references_resolve(
                [
                    RegistryReference("base tool", "weather"),
                    RegistryReference("hook tool", "absent_tool", site="hooks[0]"),
                    RegistryReference("route tool", "absent_tool"),
                ]
            )
    assert str(caught.value) == (
        "hook tool 'absent_tool' at hooks[0] is not a registered tool\n"
        "route tool 'absent_tool' is not a registered tool"
    )
