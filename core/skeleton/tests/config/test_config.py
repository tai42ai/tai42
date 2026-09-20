"""Tests for the config provider seam.

Covers the factory's built-in/convention provider resolution and dynamic-import
dispatch, that importing the factory pulls in no provider plugin, and the file
provider's contract conformance.
"""

import importlib
import subprocess
import sys
from collections.abc import Iterator

import pytest
from tai42_contract.config.manager import ConfigManager

from tai42_skeleton.config import (
    ConfigManagerFactory,
    FileConfigManager,
    build_config_manager,
)
from tai42_skeleton.config import factory as factory_mod
from tai42_skeleton.config.config_mode import ConfigMode, config_mode


@pytest.fixture(autouse=True)
def _file_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default every test to the file config mode with a clean cached accessor."""
    monkeypatch.setenv("TAI_CONFIG_MODE", "file")
    config_mode.cache_clear()
    yield
    config_mode.cache_clear()


def test_file_mode_resolves_file_manager_via_build_factory() -> None:
    """``file`` mode dynamically imports the built-in provider and calls its
    ``build_config_manager()`` factory."""
    manager = ConfigManagerFactory.create()
    assert isinstance(manager, FileConfigManager)


def test_file_provider_module_exposes_build_config_manager() -> None:
    """The built-in file provider follows the ``build_config_manager()`` convention."""
    manager = build_config_manager()
    assert isinstance(manager, FileConfigManager)


def test_provider_module_resolves_builtins_and_convention() -> None:
    """``file`` is built into the skeleton; every other mode resolves by convention
    to ``tai42_config_<mode>.manager``, naming no plugin in the factory."""
    assert factory_mod._provider_module("file") == "tai42_skeleton.config.file_manager"
    assert factory_mod._provider_module("external") == "tai42_config_external.manager"
    assert factory_mod._provider_module("vault") == "tai42_config_vault.manager"


def test_factory_imports_no_provider_plugin() -> None:
    """Importing the factory pulls in no config provider plugin — proven in a clean
    interpreter so the check is independent of ambient imports (under a shared venv
    another package may already have imported a provider in-process)."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tai42_skeleton.config.factory\n"
            "assert not [m for m in sys.modules if m.startswith('tai42_config_')]\n",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_absent_provider_mode_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mode whose provider plugin is not installed raises ImportError loudly rather
    than degrading to a default provider."""
    monkeypatch.setattr(factory_mod, "config_mode", lambda: "vault")
    with pytest.raises(ImportError, match="config mode 'vault'"):
        ConfigManagerFactory.create()


def test_factory_dispatches_via_dynamic_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory imports the mapped module and calls its ``build_config_manager``."""
    sentinel = object()
    fake = importlib.import_module("tai42_skeleton.config.file_manager")
    monkeypatch.setattr(fake, "build_config_manager", lambda: sentinel)
    assert ConfigManagerFactory.create() is sentinel


def test_file_manager_satisfies_contract() -> None:
    """``FileConfigManager`` is a concrete :class:`ConfigManager` (all abstracts implemented)."""
    assert issubclass(FileConfigManager, ConfigManager)
    manager = FileConfigManager()
    assert isinstance(manager, ConfigManager)


def test_config_mode_default_is_file() -> None:
    """With ``TAI_CONFIG_MODE=file`` the accessor returns the ``file`` string value."""
    assert config_mode() == ConfigMode.file.value == "file"
