"""Tests for the k8s config provider plugin.

Covers contract conformance of :class:`K8sConfigManager`, the
``build_config_manager()`` factory convention, and the plugin-scoped install
hint raised when the ``kubernetes`` extra is absent.
"""

import sys

import pytest
from tai42_contract.config.manager import ConfigManager

from tai42_config_k8s import build_config_manager
from tai42_config_k8s import settings as settings_mod
from tai42_config_k8s._kubernetes_optional import require_kubernetes
from tai42_config_k8s.manager import K8sConfigManager


@pytest.fixture(autouse=True)
def _no_service_account_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the service-account lookup at a missing file so namespace resolves to 'default'."""
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")


def test_manager_is_concrete_config_manager() -> None:
    """``K8sConfigManager`` implements the ``ConfigManager`` ABC (all abstracts)."""
    assert issubclass(K8sConfigManager, ConfigManager)
    assert isinstance(K8sConfigManager(), ConfigManager)


def test_build_config_manager_returns_k8s_manager() -> None:
    """The provider factory convention yields a ``K8sConfigManager``."""
    manager = build_config_manager()
    assert isinstance(manager, K8sConfigManager)
    assert isinstance(manager, ConfigManager)


def test_require_kubernetes_passes_when_installed() -> None:
    """With the ``kubernetes`` client present the guard is a no-op."""
    assert require_kubernetes() is None


def test_require_kubernetes_raises_plugin_scoped_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``kubernetes`` is missing the guard raises ImportError naming this plugin's extra."""
    monkeypatch.setitem(sys.modules, "kubernetes", None)
    with pytest.raises(ImportError, match=r"tai42-config-k8s\[k8s\]"):
        require_kubernetes()


def test_manager_construction_requires_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manager runs the guard in ``__init__``, surfacing the missing extra."""
    monkeypatch.setitem(sys.modules, "kubernetes", None)
    with pytest.raises(ImportError, match=r"tai42-config-k8s\[k8s\]"):
        K8sConfigManager()
