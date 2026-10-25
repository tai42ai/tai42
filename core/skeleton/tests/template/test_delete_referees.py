"""The template-delete referee registry and the gather the delete gate reads from it."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.holders import template_delete_holders
from tai42_skeleton.template.delete_referees import TemplateDeleteRefereeRegistry
from tai42_skeleton.template.platform_referees import PLATFORM_TEMPLATE_REFEREES

from ..app._fixtures.reload import reload_with


def _holder(name: str) -> ReferenceHolder:
    return ReferenceHolder(kind="hook", name=name, summary=f"hook {name!r} names 'jq/gate' at condition")


async def _first(ids: frozenset[str]) -> list[ReferenceHolder]:
    return [_holder("a")] if "jq/gate" in ids else []


async def _second(ids: frozenset[str]) -> list[ReferenceHolder]:
    return [_holder("b")] if "jq/gate" in ids else []


async def _not_a_list(ids: frozenset[str]) -> object:
    return _holder("c")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> TemplateDeleteRefereeRegistry:
    fresh = TemplateDeleteRefereeRegistry()
    monkeypatch.setattr(instance.app._serving_core, "_template_delete_referee_registry", fresh)
    return fresh


def test_the_registry_answers_in_registration_order_and_snapshots() -> None:
    reg = TemplateDeleteRefereeRegistry()
    reg.register(_second)
    reg.register(_first)
    assert reg.all() == [_second, _first]
    reg.all().clear()
    assert reg.all() == [_second, _first]


def test_a_second_registration_of_one_referee_raises() -> None:
    reg = TemplateDeleteRefereeRegistry()
    reg.register(_first)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_first)


def test_reset_clears_the_registry() -> None:
    reg = TemplateDeleteRefereeRegistry()
    reg.register(_first)
    reg.reset()
    assert reg.all() == []


def test_the_storage_facet_registers_into_the_registry(registry: TemplateDeleteRefereeRegistry) -> None:
    instance.app.storage.register_template_delete_referee(_first)
    assert registry.all() == [_first]
    assert instance.app.storage.template_delete_referees() == [_first]


def test_each_epoch_holds_the_platform_referees_once_and_drops_a_plugins() -> None:
    manifest = Manifest.model_validate({})

    async def run() -> None:
        async with instance.app.app_context(manifest):
            assert instance.app.storage.template_delete_referees() == list(PLATFORM_TEMPLATE_REFEREES)
            instance.app.storage.register_template_delete_referee(_first)
            await reload_with(instance.app, manifest)
            assert instance.app.storage.template_delete_referees() == list(PLATFORM_TEMPLATE_REFEREES)

    asyncio.run(run())


async def test_the_gather_concatenates_every_referees_answer_in_registration_order(
    registry: TemplateDeleteRefereeRegistry,
) -> None:
    registry.register(_second)
    registry.register(_first)
    assert await template_delete_holders(frozenset({"jq/gate"})) == [_holder("b"), _holder("a")]
    assert await template_delete_holders(frozenset({"jq/other"})) == []


async def test_a_referee_answering_a_non_list_raises_a_type_error_naming_it(
    registry: TemplateDeleteRefereeRegistry,
) -> None:
    registry.register(_not_a_list)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"referee .*_not_a_list answered .*a referee answers a list"):
        await template_delete_holders(frozenset({"jq/gate"}))
