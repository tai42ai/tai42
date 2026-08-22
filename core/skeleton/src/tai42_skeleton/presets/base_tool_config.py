"""Per-base-tool preset-authoring declarations — the bodies behind
``app.presets.register_input_schema_support`` / ``register_registration_tier``.

A base-tool plugin declares these under its base-tool name when its tool module loads,
exactly like the write-validator registry: the input-schema support names the base-tool
argument a preset's validated structured input is routed into, and the registration tier
names the authz character required to author a preset over the base tool. Both registries
are reset on every ``start()`` so a reload re-imports the tool modules and re-registers
cleanly; a duplicate name within one load raises loudly (a silent overwrite could swap a
base tool's authoring contract out from under it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.presets import PresetInputSchemaSupport

if TYPE_CHECKING:
    from tai42_skeleton.app.route_registry import RouteAction


class PresetInputSchemaSupportRegistry:
    def __init__(self) -> None:
        self._supports: dict[str, PresetInputSchemaSupport] = {}

    def register(self, base_tool: str, support: PresetInputSchemaSupport) -> None:
        if base_tool in self._supports:
            raise ValueError(f"preset input-schema support for base tool {base_tool!r} is already registered")
        self._supports[base_tool] = support

    def get(self, base_tool: str) -> PresetInputSchemaSupport | None:
        return self._supports.get(base_tool)

    def reset(self) -> None:
        self._supports.clear()


class PresetRegistrationTierRegistry:
    def __init__(self) -> None:
        self._tiers: dict[str, RouteAction] = {}

    def register(self, base_tool: str, tier: RouteAction) -> None:
        if base_tool in self._tiers:
            raise ValueError(f"preset registration tier for base tool {base_tool!r} is already registered")
        self._tiers[base_tool] = tier

    def get(self, base_tool: str) -> RouteAction | None:
        return self._tiers.get(base_tool)

    def reset(self) -> None:
        self._tiers.clear()
