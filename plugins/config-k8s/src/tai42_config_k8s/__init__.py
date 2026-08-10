"""tai42-config-k8s — the Kubernetes ``ConfigManager`` provider plugin.

Ships :class:`K8sConfigManager`, an implementation of the
:class:`tai42_contract.config.manager.ConfigManager` ABC that reads/writes
environment configuration via K8s Secrets and manifest configuration via K8s
ConfigMaps, plus its ``K8sConfigSettings`` and the ``build_config_manager()``
factory the skeleton's config seam loads by dynamic import.
"""

from tai42_config_k8s.manager import (
    K8sConfigError,
    K8sConfigManager,
    build_config_manager,
)
from tai42_config_k8s.settings import K8sConfigSettings, k8s_config_settings

__all__ = [
    "K8sConfigError",
    "K8sConfigManager",
    "K8sConfigSettings",
    "build_config_manager",
    "k8s_config_settings",
]
