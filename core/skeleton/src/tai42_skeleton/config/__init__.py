"""Configuration provider seam — factory + the built-in ``file`` provider.

This package implements :class:`~tai42_contract.config.manager.ConfigManager` as a
pluggable-provider feature. It ships the selection seam (``ConfigMode`` +
``ConfigModeSettings`` + the ``ConfigManagerFactory`` naming convention) and the
default :class:`FileConfigManager`. Other providers ship as separately-installed
plugins exposing the same ``build_config_manager()`` convention; the factory
resolves ``TAI_CONFIG_MODE`` to ``tai42_config_<mode>.manager`` and loads it by
dynamic import.

Usage::

    from tai42_skeleton.config import ConfigManagerFactory
    manager = ConfigManagerFactory.create()
"""

from tai42_skeleton.config.config_mode import (
    ConfigMode,
    ConfigModeSettings,
    config_mode,
)
from tai42_skeleton.config.factory import ConfigManagerFactory
from tai42_skeleton.config.file_manager import FileConfigManager, build_config_manager

__all__ = [
    "ConfigManagerFactory",
    "ConfigMode",
    "ConfigModeSettings",
    "FileConfigManager",
    "build_config_manager",
    "config_mode",
]
