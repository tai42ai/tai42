"""The process-wide preset write-validator registry — the body behind
``app.presets.register_write_validator``.

A base-tool plugin registers a validator under its base-tool name when its tool
module loads (importing the module runs its
``tai42_app.presets.register_write_validator(...)`` call). The preset write path
consults the registered validator for a body's ``base_tool`` before persisting.

The registry is reset on every ``start()`` (like the agent binding) so a reload
re-imports the tool modules and re-registers cleanly; a duplicate name within one
load raises loudly (a silent overwrite could swap a base tool's write gate out
from under it).
"""

from __future__ import annotations

from tai42_contract.presets import PresetWriteValidator


class PresetWriteValidatorRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, PresetWriteValidator] = {}

    def register(self, base_tool: str, validator: PresetWriteValidator) -> None:
        if base_tool in self._validators:
            raise ValueError(f"preset write validator for base tool {base_tool!r} is already registered")
        self._validators[base_tool] = validator

    def get(self, base_tool: str) -> PresetWriteValidator | None:
        return self._validators.get(base_tool)

    def reset(self) -> None:
        self._validators.clear()
