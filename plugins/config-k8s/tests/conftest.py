"""Shared fixtures for the tai42-config-k8s test suite.

Every test builds settings from a hermetic environment: the autouse fixture
strips any ambient ``TAI_K8S_*`` overrides and resets the cached
``k8s_config_settings()`` accessor around each test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tai42_config_k8s.settings import k8s_config_settings

_ENV_VARS = (
    "TAI_K8S_NAMESPACE",
    "TAI_K8S_SECRET_NAME",
    "TAI_K8S_CONFIGMAP_NAME",
    "TAI_K8S_MANIFEST_KEY",
    "TAI_K8S_DEFAULTS_MANIFEST_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env_and_settings_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip any ambient TAI_K8S_* env vars and reset the cached settings accessor
    so each test builds settings from a known-empty environment."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    k8s_config_settings.cache_clear()
    yield
    k8s_config_settings.cache_clear()
