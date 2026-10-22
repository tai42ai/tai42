"""OCI backend settings: the ``STORAGE_OCI_`` env group.

``bucket`` and ``namespace`` address the store; the rest form the connection.
No credential field exists: the backend authenticates as a principal, so a
static key has nowhere to be configured.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class OciSettings(TaiBaseSettings):
    """OCI backend settings, read from the ``STORAGE_OCI_`` env prefix."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_OCI_")

    bucket: str | None = None
    # The Object Storage namespace. Unset resolves it once per client from the
    # tenancy the principal belongs to, which costs one extra call per client.
    namespace: str | None = None
    region: str | None = None
    # workload_identity is the only mode that needs no credential material: the
    # pod's projected service-account token is exchanged for a resource
    # principal. instance_principal covers a VM outside Kubernetes, config_file
    # a developer machine holding ~/.oci/config.
    auth: Literal["workload_identity", "instance_principal", "config_file"] = "workload_identity"
    # Where the projected service-account token is mounted. Unset leaves the
    # SDK's own default path.
    service_account_token_path: str | None = None
    connect_timeout: int = 5
    read_timeout: int = 30


@settings_cache
def oci_settings() -> OciSettings:
    """Return the cached OCI backend settings."""
    return OciSettings()
