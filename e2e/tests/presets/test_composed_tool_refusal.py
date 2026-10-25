"""A preset over a base tool that declares a ``tool_refs`` extractor is checked against the
live registry at create: every composed tool its kwargs name must be served.

The fixture ``e2e_composer`` names each ``steps`` entry as a composed tool sited by its
position, so a create naming an unserved step is a 400 that names it at its step and stores
nothing, while a create naming only served tools stores the preset."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:versioning", "probe-tools", "setting:tai42_e2e_fixtures.seams")


def _composer_body(name: str, steps: list[str]) -> dict:
    return {
        "name": name,
        "base_tool": "e2e_composer",
        "description": "composing preset",
        "fixed_kwargs": {"steps": steps},
    }


async def test_create_refuses_an_unserved_composed_tool(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = seams_stack.api()
    name = uniq("composing")
    ghost = uniq("ghost")

    refused = await api.request_raw("POST", "/api/presets", json=_composer_body(name, ["e2e_echo", ghost]))
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == f"composed tool {ghost!r} at step 1 is not a registered tool"
    assert name not in {row["name"] for row in await api.get("/api/presets")}

    created = await api.post("/api/presets", json=_composer_body(name, ["e2e_echo"]), retry_on_reloading=True)
    assert created["name"] == name
