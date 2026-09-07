"""Per-base-tool preset input-schema support — the body behind
``app.presets.register_input_schema_support``.

A base-tool plugin declares it under its base-tool name when its tool module loads,
exactly like the write-validator registry: the input-schema support names the base-tool
argument a preset's validated structured input is routed into. The registry is reset on
every ``start()`` so a reload re-imports the tool modules and re-registers cleanly; a
duplicate name within one load raises loudly (a silent overwrite could swap a base tool's
authoring contract out from under it). The registration tier — shared with the run-time
fence — lives on the tools facet (``tai42_skeleton.tools.tier``).
"""

from __future__ import annotations

from tai42_contract.presets import PresetInputSchemaSupport


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
