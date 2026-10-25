"""The preset write doors dry-run bind every extension combo on the baked preset before storing.

The app serves the preset fixtures' ``echo`` and the extensions of
:mod:`tests.app._fixtures.ext_check`: ``refusing`` refuses the setting ``label == "x"`` with
``bad label`` and ``labelled`` declares a required, non-empty ``label``. Create, save-version,
rollback and the dry-run validate door each answer a refused combo with the bind's own text,
and nothing is stored.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _MANIFEST

_EXTENSIONS_MODULE = "tests.app._fixtures.ext_check"
_REFUSED = {"name": "refusing", "config": {"label": "x"}}
_ACCEPTED = {"name": "refusing", "config": {"label": "ok"}}
_MISSING_LABEL = {"name": "labelled", "config": {}}
_REFUSAL = "extension 'refusing' on tool 'p' cannot bind: bad label"
_SCHEMA_REFUSAL = (
    "extension 'labelled' on tool 'p' cannot bind: extension 'labelled' config: value does not match schema at $: "
    "breaks the schema's 'required' rule: 'label' (the value is not shown)"
)


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {**_MANIFEST, "extensions_modules": [*_MANIFEST["extensions_modules"], _EXTENSIONS_MODULE]}
    )


def _booted(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await body()

    asyncio.run(run())


def _factory_calls() -> list[str]:
    return sys.modules[_EXTENSIONS_MODULE].CALLS


async def _create(extensions: list[list[Any]]) -> None:
    await preset_ops.create_preset(
        name="p", base_tool="echo", description="d", fixed_kwargs={}, extensions=extensions, output_schema=None
    )


async def _save(extensions: list[list[Any]]) -> None:
    await preset_ops.save_version(
        name="p",
        fixed_kwargs=None,
        extensions=extensions,
        output_schema=None,
        output_schema_provided=False,
        description=None,
    )


async def _active_version() -> int:
    return (await instance.app.presets.store.get_preset("p")).active_version


@pytest.mark.parametrize(("combo", "refusal"), [(_REFUSED, _REFUSAL), (_MISSING_LABEL, _SCHEMA_REFUSAL)])
def test_create_refuses_a_combo_the_bind_refuses_before_storing(pg, combo: dict[str, Any], refusal: str) -> None:
    async def body() -> None:
        with pytest.raises(preset_ops.BadRequestError) as caught:
            await _create([[combo]])
        assert str(caught.value) == refusal
        assert await instance.app.presets.store.list_presets() == []
        assert "p" not in instance.app.preset_manager.registered_names()

    _booted(body)


@pytest.mark.parametrize(("combo", "refusal"), [(_REFUSED, _REFUSAL), (_MISSING_LABEL, _SCHEMA_REFUSAL)])
def test_save_version_refuses_a_combo_the_bind_refuses_before_storing(pg, combo: dict[str, Any], refusal: str) -> None:
    async def body() -> None:
        await _create([])
        with pytest.raises(preset_ops.BadRequestError) as caught:
            await _save([[combo]])
        assert str(caught.value) == refusal
        assert [row.version for row in await instance.app.presets.store.list_versions("p")] == [1]
        assert await _active_version() == 1
        assert "p" in instance.app.preset_manager.registered_names()

    _booted(body)


@pytest.mark.parametrize(("combo", "refusal"), [(_REFUSED, _REFUSAL), (_MISSING_LABEL, _SCHEMA_REFUSAL)])
def test_rollback_refuses_a_stored_version_whose_combo_the_bind_refuses(
    pg, combo: dict[str, Any], refusal: str
) -> None:
    async def body() -> None:
        await _create([])
        # The refused combo reaches the store only when written to it directly.
        await instance.app.presets.store.save_version("p", extensions=[[combo]])
        await _save([])
        assert await _active_version() == 3
        with pytest.raises(preset_ops.BadRequestError) as caught:
            await preset_ops.rollback_preset(name="p", version=2)
        assert str(caught.value) == refusal
        assert await _active_version() == 3
        assert "p" in instance.app.preset_manager.registered_names()

    _booted(body)


@pytest.mark.parametrize(("combo", "refusal"), [(_REFUSED, _REFUSAL), (_MISSING_LABEL, _SCHEMA_REFUSAL)])
def test_the_validate_door_reports_the_refusal_in_both_modes(pg, combo: dict[str, Any], refusal: str) -> None:
    async def body() -> None:
        created = await preset_ops.validate_preset(
            name="p", base_tool="echo", description="d", extensions_present=True, extensions_value=[[combo]]
        )
        assert created["valid"] is False
        assert created["error"] == refusal

        await _create([])
        versioned = await preset_ops.validate_preset(name="p", extensions_present=True, extensions_value=[[combo]])
        assert versioned["valid"] is False
        assert versioned["error"] == refusal

    _booted(body)


def test_a_combo_the_bind_accepts_passes_every_door(pg) -> None:
    async def body() -> None:
        verdict = await preset_ops.validate_preset(
            name="p", base_tool="echo", description="d", extensions_present=True, extensions_value=[[_ACCEPTED]]
        )
        assert verdict["valid"] is True
        _factory_calls().clear()
        await _create([[_ACCEPTED]])
        # The dry run on the baked preset and the register after the store each run the factory once.
        assert _factory_calls() == ["refusing", "refusing"]
        await _save([[_ACCEPTED]])
        await preset_ops.rollback_preset(name="p", version=1)
        assert await _active_version() == 1

    _booted(body)


def test_a_bake_refusal_still_reads_as_the_preset_bind_refusal(pg) -> None:
    async def body() -> None:
        with pytest.raises(preset_ops.BadRequestError) as caught:
            await preset_ops.create_preset(
                name="p",
                base_tool="echo",
                description="d",
                fixed_kwargs={"not_a_param": 1},
                extensions=[[_REFUSED]],
                output_schema=None,
            )
        assert str(caught.value).startswith("preset 'p' cannot bind: ")

    _booted(body)
