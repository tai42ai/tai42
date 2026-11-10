"""The ``COMPUTE_`` settings group: how many worker processes the process's compute pool runs."""

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache


class ComputeSettings(TaiBaseSettings):
    """Process-wide compute pool settings, read from ``COMPUTE_``-prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="COMPUTE_")

    workers: int = Field(
        default=2,
        ge=1,
        description=(
            "The number of worker processes in each platform process's compute pool. A job waits for a free "
            "worker when every worker is busy. The pool is sized when it starts, so a change converges through "
            "a process recycle."
        ),
        json_schema_extra={"reload": "recycle"},
    )


@settings_cache
def compute_settings() -> ComputeSettings:
    """Return the process-wide :class:`ComputeSettings`, cached after first load."""
    return ComputeSettings()
