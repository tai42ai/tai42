"""Build a Langfuse monitoring backend from env or explicit project configs."""

from __future__ import annotations

from tai42_contract.monitoring import Monitoring, ProjectConfig

from tai42_monitoring_langfuse.monitoring import LangfuseMonitoring
from tai42_monitoring_langfuse.settings import langfuse_settings


def build_langfuse_backend(
    projects: list[ProjectConfig] | None = None,
    default_public_key: str | None = None,
) -> Monitoring:
    """Construct a ``LangfuseMonitoring``.

    With no explicit ``projects``, reads the single project from the
    ``LANGFUSE_*`` environment and raises if credentials are unset (never a
    silent no-op). The client builds lazily on first use, never at import.
    """
    if projects is None:
        settings = langfuse_settings()
        if not (settings.public_key and settings.secret_key and settings.host):
            raise RuntimeError(
                "Langfuse monitoring is selected (monitoring_module points at the "
                "Langfuse plugin) but LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / "
                "LANGFUSE_HOST are not all set. Provide the credentials, or remove "
                "the monitoring_module to run without monitoring."
            )
        config = settings.to_project_config()
        projects = [config]
        default_public_key = config.public_key
    if default_public_key is None:
        default_public_key = projects[0].public_key
    return LangfuseMonitoring(projects=projects, default_public_key=default_public_key)
