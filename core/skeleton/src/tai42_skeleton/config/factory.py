"""ConfigManagerFactory — selects the active config provider at startup.

Config is read before the manifest, so a provider cannot load through the manifest
mechanism other features use. The factory resolves the mode to a provider module —
``file`` is built in, every other mode resolves by convention to
``tai42_config_<mode>.manager`` — imports it, and calls its ``build_config_manager()``.
No plugin is named here, so the skeleton carries no dependency on any config plugin;
an absent provider raises loudly.
"""

from __future__ import annotations

import importlib
import importlib.util

from tai42_contract.config.manager import ConfigManager

from tai42_skeleton.config.config_mode import config_mode

# Modes built into the skeleton; every other mode resolves by convention below.
_BUILTIN_PROVIDERS: dict[str, str] = {"file": "tai42_skeleton.config.file_manager"}


def _provider_module(mode: str) -> str:
    return _BUILTIN_PROVIDERS.get(mode, f"tai42_config_{mode}.manager")


class ConfigManagerFactory:
    """Resolves the active :class:`ConfigManager` for the current config mode."""

    @staticmethod
    def create() -> ConfigManager:
        """Build the :class:`ConfigManager` for the current config mode.

        Raises:
            ImportError: If the selected provider is not installed, a loud install
                hint. A provider that IS installed but fails its own import raises
                that error unwrapped, so a broken provider is never mislabeled absent.
        """
        mode = config_mode()
        module_name = _provider_module(mode)
        try:
            found = importlib.util.find_spec(module_name) is not None
        except ModuleNotFoundError:
            found = False
        if not found:
            raise ImportError(
                f"config mode {mode!r} needs the {module_name.rsplit('.', 1)[0]!r} provider "
                f"(install tai42-config-{mode}); it is not present in this deployment."
            )
        module = importlib.import_module(module_name)
        return module.build_config_manager()
