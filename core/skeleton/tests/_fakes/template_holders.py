"""Template-delete referees for the delete doors' tests: a hook and a preset naming ``jq/gate``."""

from __future__ import annotations

import pytest
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.operations import OperationFailedError
from tai42_skeleton.template.delete_referees import TemplateDeleteRefereeRegistry

HELD_ID = "jq/gate"

HOOK_HOLDER = ReferenceHolder(
    kind="hook",
    name="h",
    locator={"template": HELD_ID, "site": "condition"},
    summary="hook 'h' names 'jq/gate' at condition",
)
PRESET_HOLDER = ReferenceHolder(
    kind="preset",
    name="p",
    locator={"template": HELD_ID, "site": "system_prompt"},
    summary="preset 'p' names 'jq/gate' at system_prompt",
)

HELD_TEMPLATE_REFUSAL = (
    "stored template 'jq/gate' cannot be deleted — held by:\n"
    "- hook 'h' names 'jq/gate' at condition\n"
    "- preset 'p' names 'jq/gate' at system_prompt\n"
    "remove those references first"
)
HELD_DIRECTORY_REFUSAL = (
    "directory 'jq' cannot be deleted — held by:\n"
    "- hook 'h' names 'jq/gate' at condition\n"
    "- preset 'p' names 'jq/gate' at system_prompt\n"
    "remove those references first"
)
HELD_HOLDERS = [HOOK_HOLDER.model_dump(), PRESET_HOLDER.model_dump()]

STORED_PRESET_FAULT = OperationFailedError("stored preset 'p': its tool arguments cannot be read")


async def hook_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    return [HOOK_HOLDER] if HELD_ID in ids else []


async def preset_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    return [PRESET_HOLDER] if HELD_ID in ids else []


async def value_error_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    raise ValueError("bad value 'x'")


async def stored_preset_fault_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    raise STORED_PRESET_FAULT


def fresh_referees(monkeypatch: pytest.MonkeyPatch) -> TemplateDeleteRefereeRegistry:
    """Install an empty template-delete referee registry on the app for one test."""
    registry = TemplateDeleteRefereeRegistry()
    monkeypatch.setattr(instance.app._serving_core, "_template_delete_referee_registry", registry)
    return registry


def hold_jq_gate(monkeypatch: pytest.MonkeyPatch) -> TemplateDeleteRefereeRegistry:
    """A registry whose hook ``h`` and preset ``p`` name ``jq/gate``."""
    registry = fresh_referees(monkeypatch)
    registry.register(hook_referee)
    registry.register(preset_referee)
    return registry
