"""Reload-class + key-material declarations on the core-owned settings groups.

Each core settings class carries a reload disposition the profile boundary reads:
``hot`` (default, re-read live), ``recycle`` (converges only via a process
respawn), or ``excluded`` (deployment/bootstrap identity a profile can never
carry). Key material is flagged separately so a profile carrying it is refused.
These assertions pin the declarations through the same registry extraction the
settings-schema API serves, so a drift is caught here rather than at apply.
"""

from __future__ import annotations

import pytest
from tai42_kit.settings import SettingsClassInfo, SettingsFieldInfo, registered_settings

# Import each module so its settings classes self-register in the kit registry.
import tai42_skeleton.app.bus_settings
import tai42_skeleton.backend.settings
import tai42_skeleton.config.config_mode
import tai42_skeleton.connectors.settings
import tai42_skeleton.marketplace.prefix
import tai42_skeleton.routers.metrics_settings
import tai42_skeleton.settings.settings  # noqa: F401


def _class(name: str) -> SettingsClassInfo:
    classes = {c.name: c for c in registered_settings()}
    assert name in classes, f"{name} is not registered"
    return classes[name]


def _field(cls_name: str, field_name: str) -> SettingsFieldInfo:
    for field in _class(cls_name).fields:
        if field.name == field_name:
            return field
    raise AssertionError(f"{cls_name}.{field_name} not found")


@pytest.mark.parametrize(
    ("cls_name", "expected"),
    [
        ("BusRedisSettings", "recycle"),
        ("BusSettings", "recycle"),
        ("ConfigModeSettings", "excluded"),
        ("PluginPrefixSettings", "excluded"),
    ],
)
def test_class_level_reload_class(cls_name: str, expected: str) -> None:
    assert _class(cls_name).reload_class == expected


def test_class_level_default_stays_hot() -> None:
    # A group with no declaration inherits the kit default; the K crypto group is
    # classified by its key-material fields, not a class-level reload_class.
    assert _class("ConnectorCryptoSecrets").reload_class == "hot"


@pytest.mark.parametrize(
    ("cls_name", "field_name", "expected"),
    [
        ("CoreSettings", "backend", "recycle"),
        ("CoreSettings", "template", "recycle"),
        ("CoreSettings", "sandbox", "recycle"),
        # All four sandbox security-as-config knobs are recycle-class (never hot): the
        # policy is bound to the kit ONCE at provider registration, so a hot change would
        # leave the kit enforcing the boot snapshot while the identity door reports the new
        # value — recycle re-imports the scalar module and re-binds the resolved policy.
        ("CoreSettings", "sandbox_egress", "recycle"),
        ("CoreSettings", "sandbox_isolation", "recycle"),
        ("CoreSettings", "sandbox_scrub_transcript", "recycle"),
        ("CoreSettings", "sandbox_durable", "recycle"),
        ("CoreSettings", "manifest_path", "excluded"),
        ("CoreSettings", "mcp_probe_timeout", "hot"),
        ("BackendSettings", "manifest_key", "recycle"),
        ("BackendSettings", "tool_name_arg", "recycle"),
        ("BackendSettings", "task_timeout", "hot"),
        ("AppArgsSettings", "transport", "excluded"),
        ("AppArgsSettings", "host", "excluded"),
        ("AppArgsSettings", "port", "excluded"),
        ("AppArgsSettings", "uds", "excluded"),
        ("AppArgsSettings", "timeout_graceful_shutdown", "recycle"),
        ("MetricsSettings", "backend_metrics_host", "excluded"),
        ("MetricsSettings", "backend_metrics_port", "excluded"),
        ("MetricsSettings", "prometheus_multiproc_dir", "excluded"),
        # The whole ConfigMode group is excluded, so both fields inherit it.
        ("ConfigModeSettings", "config_mode", "excluded"),
        ("ConfigModeSettings", "config_dir_path", "excluded"),
    ],
)
def test_field_level_reload_class(cls_name: str, field_name: str, expected: str) -> None:
    assert _field(cls_name, field_name).reload_class == expected


def test_config_dir_path_registers_the_bare_read_key() -> None:
    # The env var must be exactly the key file_manager reads, or the boundary
    # would guard a phantom key and leave the real one plantable.
    assert _field("ConfigModeSettings", "config_dir_path").env_var == "TAI_CONFIG_DIR_PATH"


@pytest.mark.parametrize("field_name", ["kek", "state_hmac_key"])
def test_key_material_fields_flagged_and_secret(field_name: str) -> None:
    field = _field("ConnectorCryptoSecrets", field_name)
    assert field.key_material is True
    assert field.secret is True  # key material is a SecretStr, so masking still applies
    assert field.default is None  # a secret default is never emitted on the wire
