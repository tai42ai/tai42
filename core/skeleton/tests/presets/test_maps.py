"""The preset map rules: both texts resolved, the input map paired with an input schema, both compiled."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.utils.render import TextBodyError

from tai42_skeleton.presets.maps import (
    INPUT_MAP_VARIABLES,
    OUTPUT_MAP_VARIABLES,
    PresetMapError,
    resolve_preset_maps,
)


class _Manager:
    def __init__(self) -> None:
        self.resources: dict[str, str] = {"in": "{city: .where, unit: $fixed.unit}", "out": ".temp"}

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        if text.id is not None:
            return self.resources[text.id]
        return text.content or ""


@pytest.fixture
def manager():
    fake = _Manager()
    app: Any = SimpleNamespace(storage=SimpleNamespace(resource_manager=fake))
    with tai42_app.bound(app):
        yield fake


def test_the_declared_variables() -> None:
    assert INPUT_MAP_VARIABLES == ("fixed",)
    assert OUTPUT_MAP_VARIABLES == ()


async def test_no_maps_resolve_to_none(manager: _Manager) -> None:
    assert await resolve_preset_maps("p", None, None, has_input_schema=False) == (None, None)


async def test_text_and_stored_maps_resolve_and_compile(manager: _Manager) -> None:
    assert await resolve_preset_maps("p", TemplatedText(id="in"), TemplatedText(id="out"), has_input_schema=True) == (
        "{city: .where, unit: $fixed.unit}",
        ".temp",
    )
    assert await resolve_preset_maps("p", None, "{mapped: .}", has_input_schema=False) == (None, "{mapped: .}")


async def test_an_input_map_needs_an_input_schema(manager: _Manager) -> None:
    with pytest.raises(PresetMapError) as caught:
        await resolve_preset_maps("p", ".", None, has_input_schema=False)
    assert str(caught.value) == "an input map needs an input schema that describes what the caller sends"


async def test_an_input_map_that_does_not_compile(manager: _Manager) -> None:
    with pytest.raises(PresetMapError, match=r"^input_map does not compile: .*\$nope is not defined"):
        await resolve_preset_maps("p", ".a + $nope", None, has_input_schema=True)
    with pytest.raises(PresetMapError, match=r"^input_map does not compile: "):
        await resolve_preset_maps("p", "{a: ", None, has_input_schema=True)


async def test_an_output_map_cannot_read_fixed(manager: _Manager) -> None:
    with pytest.raises(PresetMapError, match=r"^output_map does not compile: .*\$fixed is not defined"):
        await resolve_preset_maps("p", None, ".a + $fixed.b", has_input_schema=False)


async def test_an_unrenderable_or_empty_map_is_a_text_body_error(manager: _Manager) -> None:
    with pytest.raises(TextBodyError, match=r"^preset 'p' input_map: its stored id 'absent' could not be rendered"):
        await resolve_preset_maps("p", TemplatedText(id="absent"), None, has_input_schema=True)
    with pytest.raises(TextBodyError, match=r"^preset 'p' output_map is empty; send null to clear it$"):
        await resolve_preset_maps("p", None, "  ", has_input_schema=False)
