"""Tests for ``K8sConfigSettings`` and the cached ``k8s_config_settings()`` accessor.

Covers the field defaults, namespace resolution (service-account detection,
fallback, and the explicit-override path that skips detection), env-var overrides,
and the tai42-kit settings cache (same instance until cleared).
"""

from __future__ import annotations

import pytest

from tai42_config_k8s import settings as settings_mod
from tai42_config_k8s.settings import K8sConfigSettings, k8s_config_settings


def test_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # No service-account namespace file -> detection falls back to "default".
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    settings = K8sConfigSettings()
    assert settings.secret_name == "tai-env"
    assert settings.configmap_name == "tai-manifest"
    assert settings.manifest_key == "manifest.yml"
    assert settings.defaults_manifest_key == "defaults.manifest.yml"
    assert settings.namespace == "default"


def test_namespace_detected_from_service_account(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ns_file = tmp_path / "namespace"
    ns_file.write_text("team-x\n")
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", ns_file)
    settings = K8sConfigSettings()
    assert settings.namespace == "team-x"


def test_namespace_detected_from_service_account_is_stripped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A whitespace-padded service-account file yields the stripped namespace."""
    ns_file = tmp_path / "namespace"
    ns_file.write_text("  myns  \n")
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", ns_file)
    settings = K8sConfigSettings()
    assert settings.namespace == "myns"


def test_whitespace_service_account_file_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A present-but-blank service-account file is treated as absent -> 'default'."""
    ns_file = tmp_path / "namespace"
    ns_file.write_text("   \n")
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", ns_file)
    settings = K8sConfigSettings()
    assert settings.namespace == "default"


def test_empty_namespace_override_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An explicit empty override means 'not chosen' -> resolve, falling back to 'default'."""
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    monkeypatch.setenv("TAI_K8S_NAMESPACE", "")
    settings = K8sConfigSettings()
    assert settings.namespace == "default"


def test_detect_namespace_propagates_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-absent OSError (e.g. PermissionError) on the SA file propagates loudly."""

    class _RaisingPath:
        def read_text(self, *args: object, **kwargs: object) -> str:
            raise PermissionError("service-account namespace file not readable")

    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", _RaisingPath())
    with pytest.raises(PermissionError):
        K8sConfigSettings()


def test_explicit_namespace_skips_detection(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An explicitly-set namespace wins; the after-validator must not overwrite it."""
    ns_file = tmp_path / "namespace"
    ns_file.write_text("from-service-account")
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", ns_file)
    monkeypatch.setenv("TAI_K8S_NAMESPACE", "explicit-ns")
    settings = K8sConfigSettings()
    assert settings.namespace == "explicit-ns"


def test_whitespace_padded_namespace_override_is_stripped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A whitespace-padded explicit override is stored stripped, not padded."""
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    monkeypatch.setenv("TAI_K8S_NAMESPACE", "  myns  ")
    settings = K8sConfigSettings()
    assert settings.namespace == "myns"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    monkeypatch.setenv("TAI_K8S_SECRET_NAME", "my-secret")
    monkeypatch.setenv("TAI_K8S_CONFIGMAP_NAME", "my-configmap")
    monkeypatch.setenv("TAI_K8S_MANIFEST_KEY", "app.yml")
    monkeypatch.setenv("TAI_K8S_DEFAULTS_MANIFEST_KEY", "app.defaults.yml")
    settings = K8sConfigSettings()
    assert settings.secret_name == "my-secret"
    assert settings.configmap_name == "my-configmap"
    assert settings.manifest_key == "app.yml"
    assert settings.defaults_manifest_key == "app.defaults.yml"


def test_settings_cache_returns_same_instance(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    first = k8s_config_settings()
    second = k8s_config_settings()
    assert first is second


def test_settings_cache_clear_rebuilds(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    monkeypatch.setenv("TAI_K8S_SECRET_NAME", "first")
    assert k8s_config_settings().secret_name == "first"

    # Without a clear the cached singleton ignores the new environment.
    monkeypatch.setenv("TAI_K8S_SECRET_NAME", "second")
    assert k8s_config_settings().secret_name == "first"

    k8s_config_settings.cache_clear()
    assert k8s_config_settings().secret_name == "second"
