"""The ``LANGFUSE_`` env prefix is owned: an env name under it that is not one of the
plugin's five settings refuses boot through the owned-prefix audit.

The settings the plugin reads are ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``,
``LANGFUSE_HOST``, ``LANGFUSE_TIMEOUT_SECONDS`` and ``LANGFUSE_TRACING_ENVIRONMENT``; the
Langfuse SDK's own export and debug settings have no effect on this plugin, so setting
one is refused rather than ignored. The audit runs over the env names a boot reads
(the process environment, the ``.env`` file, the stored env).
"""

from __future__ import annotations

import pytest
from tai42_kit.settings import UnknownOwnedSettingError, refuse_unknown_owned_env

# Importing the plugin's settings module registers its owning group with the audit.
import tai42_monitoring_langfuse.settings  # noqa: F401

_SOURCE = "boot (process environment)"

_READ_SETTINGS = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_TIMEOUT_SECONDS",
    "LANGFUSE_TRACING_ENVIRONMENT",
)


@pytest.mark.parametrize(
    "name",
    [
        "LANGFUSE_FLUSH_AT",
        "LANGFUSE_FLUSH_INTERVAL",
        "LANGFUSE_OTEL_TRACES_EXPORT_PATH",
        "LANGFUSE_MEDIA_UPLOAD_ENABLED",
        "LANGFUSE_DEBUG",
        "LANGFUSE_HOTS",
    ],
)
def test_a_name_the_plugin_does_not_read_refuses_boot_naming_it(name: str) -> None:
    environment = {**dict.fromkeys(_READ_SETTINGS, "set"), name: "1"}
    with pytest.raises(UnknownOwnedSettingError) as refused:
        refuse_unknown_owned_env(environment, source=_SOURCE)
    message = str(refused.value)
    assert message.startswith(f"{_SOURCE}: unknown setting(s) under an owned env prefix: {name} (prefix LANGFUSE_).")
    for read in _READ_SETTINGS:
        assert read not in message


def test_the_five_settings_and_the_collector_credential_pass() -> None:
    environment = {**dict.fromkeys(_READ_SETTINGS, "set"), "OTEL_COLLECTOR_LANGFUSE_AUTH": "cGs6c2s="}
    refuse_unknown_owned_env(environment, source=_SOURCE)
