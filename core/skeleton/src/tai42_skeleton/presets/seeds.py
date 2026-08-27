"""The process-wide declared-preset-seed registry — the body behind
``app.presets.register_seed``.

A plugin declares a default preset at import time (registering the seed when its
module loads); the startup/reload seed applier creates it when absent, upgrades it
when a shipped default drifts, and never touches an operator-edited preset.

Reset on every ``start()`` (like the write-validator registry) so a reload re-imports
the plugin modules and re-declares cleanly; declaring two seeds under the same ``name``
raises loudly — a silent overwrite could drop one plugin's default under another's.
"""

from __future__ import annotations

from tai42_contract.presets import PresetSeed


class PresetSeedRegistry:
    def __init__(self) -> None:
        self._seeds: dict[str, PresetSeed] = {}

    def register(self, seed: PresetSeed) -> None:
        if seed.name in self._seeds:
            raise ValueError(f"preset seed {seed.name!r} is already registered")
        self._seeds[seed.name] = seed

    def all(self) -> list[PresetSeed]:
        return list(self._seeds.values())

    def reset(self) -> None:
        self._seeds.clear()
