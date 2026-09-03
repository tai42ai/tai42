"""The process-wide declared-preset-seed registry — the body behind
``app.presets.register_seed``.

A plugin declares a default preset at import time (registering the seed when its
module loads); the startup/reload seed applier creates it when absent, upgrades it
when a shipped default drifts, and never touches an operator-edited preset. A plugin
that WITHDRAWS a seed declares the name retired instead, and the applier deletes the
deployed record if the seed still owns it.

Reset on every ``start()`` (like the write-validator registry) so a reload re-imports
the plugin modules and re-declares cleanly; declaring two seeds under the same ``name``
raises loudly — a silent overwrite could drop one plugin's default under another's —
and so does declaring one name BOTH seeded and retired (in either load order): the
applier could not honor both create and delete for the same name.
"""

from __future__ import annotations

from tai42_contract.presets import PresetSeed


class PresetSeedRegistry:
    def __init__(self) -> None:
        self._seeds: dict[str, PresetSeed] = {}
        # Insertion-ordered name set (dict keys) so retirement applies in declaration
        # order — the same determinism ``_seeds`` gives the create/upgrade pass.
        self._retired: dict[str, None] = {}

    def register(self, seed: PresetSeed) -> None:
        if seed.name in self._seeds:
            raise ValueError(f"preset seed {seed.name!r} is already registered")
        if seed.name in self._retired:
            raise ValueError(f"preset seed {seed.name!r} is declared retired — it cannot be both seeded and retired")
        self._seeds[seed.name] = seed

    def register_retired(self, name: str) -> None:
        if name in self._retired:
            raise ValueError(f"retired preset seed {name!r} is already registered")
        if name in self._seeds:
            raise ValueError(f"preset seed {name!r} is declared seeded — it cannot be both seeded and retired")
        self._retired[name] = None

    def all(self) -> list[PresetSeed]:
        return list(self._seeds.values())

    def retired(self) -> list[str]:
        return list(self._retired)

    def reset(self) -> None:
        self._seeds.clear()
        self._retired.clear()
