"""The ``TAI_BACKEND_LIVENESS_`` settings group: where the liveness file lives and how late a report may be."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache


class BackendLivenessSettings(TaiBaseSettings):
    """The liveness file a backend loop process writes and the probe reads.

    Every field is deployment identity: the process reads it once at launch and the probe reads it
    from the container's environment, so a reload never changes it.
    """

    model_config = SettingsConfigDict(env_prefix="TAI_BACKEND_LIVENESS_")

    path: Path = Field(
        default=Path("/tmp/tai-backend-liveness"),  # noqa: S108 a fixed, well-known path the probe agrees on
        description=(
            "Where this backend process writes its liveness file. One file per process: give each backend "
            "process that shares a filesystem its own path."
        ),
        json_schema_extra={"reload": "excluded"},
    )
    interval: float = Field(
        default=5.0,
        gt=0,
        description=(
            "The refresh margin, in seconds: a steady loop's report is written at most once per interval, and the "
            "interval is added to the horizon the file promises."
        ),
        json_schema_extra={"reload": "excluded"},
    )
    grace: float = Field(
        default=25.0,
        ge=0,
        description="Seconds the probe allows past the horizon the liveness file promises before it reports not alive.",
        json_schema_extra={"reload": "excluded"},
    )

    @field_validator("path")
    @classmethod
    def _path_must_be_absolute(cls, value: Path) -> Path:
        # A relative path resolves against each process's working directory, so the process and
        # the probe would disagree on the file.
        if not value.is_absolute():
            raise ValueError(
                f"path must be an absolute path, got {str(value)!r}; a relative path resolves against each "
                "process's working directory, so the process and the probe would read different files"
            )
        return value


@settings_cache
def backend_liveness_settings() -> BackendLivenessSettings:
    """The cached :class:`BackendLivenessSettings` for this process."""
    return BackendLivenessSettings()
