"""Build the Langfuse monitoring backend from the ``LANGFUSE_*`` environment."""

from __future__ import annotations

from tai42_contract.monitoring import Monitoring

from tai42_monitoring_langfuse.monitoring import LangfuseMonitoring
from tai42_monitoring_langfuse.settings import langfuse_settings


def build_langfuse_backend() -> Monitoring:
    """Construct a ``LangfuseMonitoring`` from the ``LANGFUSE_*`` environment.

    Raises when the read credentials are unset (never a silent no-op), and — through the
    kit writer — when the OpenTelemetry export environment is incomplete. The read client
    builds lazily on first use, never at import.
    """
    settings = langfuse_settings()
    if not (settings.public_key and settings.secret_key and settings.host):
        raise RuntimeError(
            "Langfuse monitoring is selected (monitoring_module points at the "
            "Langfuse plugin) but LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / "
            "LANGFUSE_HOST are not all set. Provide the credentials, or remove "
            "the monitoring_module to run without monitoring."
        )
    return LangfuseMonitoring(project=settings.to_project_config())
