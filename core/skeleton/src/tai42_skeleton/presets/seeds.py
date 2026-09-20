"""The process-wide declared-preset-seed registry — the body behind ``app.presets.register_seed``.

A plugin declares a default preset at import time (registering the seed when its
module loads); the startup/reload seed applier creates it when absent and leaves a
present preset untouched.

Reset on every ``start()`` (like the write-validator registry) so a reload re-imports
the plugin modules and re-declares cleanly; declaring two seeds under the same ``name``
raises loudly — a silent overwrite could drop one plugin's default under another's.
"""

from __future__ import annotations

from tai42_contract.presets import PresetSeed


class PresetSeedRegistry:
    """Registry of the preset seeds plugins declare at import time."""

    def __init__(self) -> None:
        """Start with an empty seed map."""
        self._seeds: dict[str, PresetSeed] = {}

    def register(self, seed: PresetSeed) -> None:
        """Register ``seed`` under its name; a duplicate name raises."""
        if seed.name in self._seeds:
            raise ValueError(f"preset seed {seed.name!r} is already registered")
        self._seeds[seed.name] = seed

    def all(self) -> list[PresetSeed]:
        """Every registered preset seed."""
        return list(self._seeds.values())

    def reset(self) -> None:
        """Drop every registered seed."""
        self._seeds.clear()
