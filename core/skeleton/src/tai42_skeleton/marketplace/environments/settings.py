"""``TAI_PLUGINS_DIR`` — the persistent directory every packaged plugin's environment is built in.

A descriptor plugin (one shipping no package) needs no directory. A packaged plugin is
installed, updated, uninstalled and rebuilt only in this directory: with it unset such an
operation refuses (:func:`require_plugins_dir`), and the plugins directory may not hold the
running environment's own site directories (:func:`refuse_environment_inside`).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import ReloadClass, TaiBaseSettings, settings_cache

from tai42_skeleton.marketplace.errors import PluginsDirError

PLUGINS_DIR_UNSET = "packaged plugins need TAI_PLUGINS_DIR — set it to a persistent, writable directory and restart"


class PluginsDirSettings(TaiBaseSettings):
    """``TAI_PLUGINS_*`` — where the packaged plugins' environments are built.

    The manifest location is already env-configurable (``TAI_MANIFEST_PATH`` on
    ``CoreSettings``), so it is not restated here.
    """

    model_config = SettingsConfigDict(env_prefix="TAI_PLUGINS_")

    # The plugin layer under this directory is put on ``sys.path`` at boot; a change takes
    # effect only on a fresh interpreter, so the group is excluded from the reload boundary.
    reload_class: ClassVar[ReloadClass] = "excluded"

    # Absolute path to the persistent plugins directory. Unset (None), the default: only
    # descriptor plugins install.
    dir: str | None = None


@settings_cache
def plugins_dir_settings() -> PluginsDirSettings:
    """Return the cached :class:`PluginsDirSettings` instance."""
    return PluginsDirSettings()


def configured_plugins_dir() -> Path | None:
    """The configured plugins directory, or ``None`` when ``TAI_PLUGINS_DIR`` is unset."""
    directory = plugins_dir_settings().dir
    return None if directory is None else Path(directory)


def require_plugins_dir(configured: Path | None) -> Path:
    """The plugins directory ``configured`` names; ``None`` raises :class:`PluginsDirError` naming the setting.

    ``configured`` is :func:`configured_plugins_dir`, or an injected stand-in for it.
    """
    if configured is None:
        raise PluginsDirError(PLUGINS_DIR_UNSET)
    return configured


def refuse_environment_inside(layer_root: Path, environment_site_dirs: Iterable[Path]) -> None:
    """Raise :class:`PluginsDirError` when a running-environment site dir resolves into ``layer_root``.

    Every distribution such a site dir holds would then read as a plugin-layer copy, never
    as the image's.
    """
    root = layer_root.resolve()
    for environment_dir in environment_site_dirs:
        if environment_dir == root or root in environment_dir.parents:
            raise PluginsDirError(
                f"TAI_PLUGINS_DIR {layer_root.parent} resolves onto the running environment ({environment_dir}); "
                "set TAI_PLUGINS_DIR to a directory outside the environment"
            )
