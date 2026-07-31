"""Kubernetes config-provider settings.

Holds the ``K8sConfigSettings`` (Secret / ConfigMap names and keys) and its
cached ``k8s_config_settings()`` accessor, built on the tai42-kit settings
machinery (``TaiBaseSettings`` + ``@settings_cache``).

Environment variables
---------------------
- ``TAI_K8S_NAMESPACE`` -- K8s namespace (default: auto-detected, see below)
- ``TAI_K8S_SECRET_NAME`` -- K8s Secret name (default: ``tai-env``)
- ``TAI_K8S_CONFIGMAP_NAME`` -- K8s ConfigMap name (default: ``tai-manifest``)
- ``TAI_K8S_MANIFEST_KEY`` -- manifest key in ConfigMap (default: ``manifest.yml``)
- ``TAI_K8S_DEFAULTS_MANIFEST_KEY`` -- defaults manifest key (default: ``defaults.manifest.yml``)
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache

logger = logging.getLogger(__name__)

_SA_NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def _detect_namespace() -> str:
    """Read the namespace from the in-cluster service account, or fall back to 'default'.

    Only an absent (or empty) service-account file falls back; any other ``OSError``
    propagates loudly.
    """
    try:
        detected = _SA_NAMESPACE_PATH.read_text().strip()
    except FileNotFoundError:
        detected = ""
    if detected:
        return detected
    logger.info("Namespace not detected from %s; falling back to 'default'", _SA_NAMESPACE_PATH)
    return "default"


class K8sConfigSettings(TaiBaseSettings):
    """Kubernetes-specific settings for Secret / ConfigMap names and keys."""

    model_config = SettingsConfigDict(
        env_prefix="TAI_K8S_",
    )

    # Empty default means "not chosen"; the validator resolves it to a non-empty value.
    namespace: str = ""
    secret_name: str = "tai-env"
    configmap_name: str = "tai-manifest"
    manifest_key: str = "manifest.yml"
    defaults_manifest_key: str = "defaults.manifest.yml"

    @model_validator(mode="after")
    def _resolve_namespace(self) -> K8sConfigSettings:
        # Unset or blank means "not chosen": resolve from the service account.
        # A padded override is stored stripped.
        stripped = self.namespace.strip()
        self.namespace = stripped if stripped else _detect_namespace()
        return self


@settings_cache
def k8s_config_settings() -> K8sConfigSettings:
    """Return the validated K8s configuration settings."""
    return K8sConfigSettings()
