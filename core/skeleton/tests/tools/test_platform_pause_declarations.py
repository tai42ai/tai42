"""The platform's own pausing tool declares it: ``ask`` can pause; the park doors that return data do not."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

tai42_app.bind(app)

_MANIFEST = {
    "tools": [
        {
            "title": "interactions",
            "module": "tai42_skeleton.tools.builtin.interactions",
            "include": ["ask", "list_parked", "resume_parked", "cancel_parked"],
        }
    ]
}


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_ask_declares_it_can_pause_and_the_park_doors_do_not():
    async def run() -> dict[str, bool]:
        async with app.app_context(Manifest.model_validate(_MANIFEST)):
            return {name: await app.tools.pauses(name) for name in _MANIFEST["tools"][0]["include"]}

    assert asyncio.run(run()) == {
        "ask": True,
        "list_parked": False,
        "resume_parked": False,
        "cancel_parked": False,
    }
