"""Langfuse connection settings: the ``LANGFUSE_`` env group.

``to_project_config`` maps the env group onto the contract's
:class:`~tai42_contract.monitoring.ProjectConfig`.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_contract.monitoring import ProjectConfig
from tai42_kit.settings import TaiBaseSettings, settings_cache


class LangfuseSettings(TaiBaseSettings):
    """Reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST``.

    ``tracing_environment`` becomes ``ProjectConfig.source``: writes are stamped
    with it and reads scoped to it, so callers sharing one project see only their
    own data.
    """

    model_config = SettingsConfigDict(env_prefix="LANGFUSE_")

    public_key: str = ""
    # SecretStr so the secret never surfaces in a repr, log, traceback, or model_dump.
    secret_key: SecretStr | None = None
    host: str = ""
    timeout_seconds: int = 30
    tracing_environment: str = "tai"

    def to_project_config(self) -> ProjectConfig:
        """The env group as a contract ``ProjectConfig``; raises if incomplete."""
        secret = self.secret_key.get_secret_value() if self.secret_key else ""
        if not (self.public_key and secret and self.host):
            raise ValueError(
                "Langfuse is selected but LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST are not all set."
            )
        return ProjectConfig(
            public_key=self.public_key,
            secret_key=secret,
            host=self.host,
            timeout_seconds=self.timeout_seconds,
            source=self.tracing_environment,
        )


@settings_cache
def langfuse_settings() -> LangfuseSettings:
    return LangfuseSettings()
