"""The lambda fixture's probe tool over its own recycle-class settings group.

Importing the module registers ``LambdaPoolSettings``, whose one field
(``E2E_LAMBDA_POOL_URL``) is recycle-class: the value a pooled resource would be built
under, read only in a fresh process. The probe returns the value the group reads."""

from __future__ import annotations

import os
from typing import ClassVar

from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.settings import ReloadClass, TaiBaseSettings, settings_cache


class LambdaPoolSettings(TaiBaseSettings):
    """The lambda fixture's pool settings, read from ``E2E_LAMBDA_*`` env."""

    model_config = SettingsConfigDict(env_prefix="E2E_LAMBDA_")
    reload_class: ClassVar[ReloadClass] = "recycle"

    pool_url: str = ""


@settings_cache
def lambda_pool_settings() -> LambdaPoolSettings:
    """The process-cached :class:`LambdaPoolSettings`."""
    return LambdaPoolSettings()


@tai42_app.tools.tool
def e2e_market_lambda_probe() -> dict:
    """Return the pool URL the lambda fixture's recycle-class settings group reads."""
    return {"marker": "e2e-market-lambda", "pool_url": lambda_pool_settings().pool_url, "pid": os.getpid()}
