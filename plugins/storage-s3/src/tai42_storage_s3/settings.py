"""S3 backend settings: the ``STORAGE_S3_`` env group.

``bucket`` is the only per-request field; the rest form the connection.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class S3Settings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="STORAGE_S3_")

    bucket: str | None = None
    endpoint: str | None = None
    # SecretStr so credentials never surface in a repr, log, traceback, or model_dump.
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    secure: bool = True
    region: str = "us-east-1"
    verify_ssl: bool = True
    connect_timeout: int = 5
    read_timeout: int = 30
    addressing_style: Literal["path", "virtual", "auto"] | None = "auto"
    request_checksum_calculation: Literal["when_supported", "when_required"] | None = None


@settings_cache
def s3_settings() -> S3Settings:
    return S3Settings()
