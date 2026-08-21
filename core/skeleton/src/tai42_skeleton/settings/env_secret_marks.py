"""Operator-marked secret env keys — a registered settings group.

The env store holds arbitrary operator-set keys the platform does not own (no
settings class declares them), so nothing knows those keys are sensitive. This
group carries the operator's own "treat these env keys as secret" marks: a
comma-separated list of env key NAMES under ``TAI_ENV_SECRET_KEYS``. It is a
``TaiBaseSettings`` subclass so the marks (a) surface in the settings-schema
view like any other group and (b) live in the env store, so config backups carry
them. Masking driven by these marks is display-side (Studio); the marks
themselves are plain data.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import NoDecode
from tai42_kit.settings import TaiBaseSettings, settings_cache


class EnvSecretMarksSettings(TaiBaseSettings):
    # Names of env keys the operator marked secret. ``NoDecode`` disables
    # pydantic-settings' JSON decode for this complex field so the raw
    # comma-separated env string reaches the ``mode="before"`` validator, which
    # splits it into a list.
    secret_keys: Annotated[list[str], NoDecode] = Field(default_factory=list, validation_alias="TAI_ENV_SECRET_KEYS")

    @field_validator("secret_keys", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept a comma-separated string (env values are strings), trimming
        whitespace and dropping empty segments; pass non-strings through."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@settings_cache
def env_secret_marks_settings() -> EnvSecretMarksSettings:
    return EnvSecretMarksSettings()


def effective_secret_keys(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """The env key names masked as secret: the stored operator marks
    (``EnvSecretMarksSettings.secret_keys``) UNIONED with every live
    ``connectors[*].client_secret_env``, deduped and sorted.

    Secret-ness of a connector's client secret is an invariant the manifest already
    STATES, so it is DERIVED here at read time rather than duplicated into the env
    store — an oauth connector's secret value stays masked even with no operator mark.
    ``manifest`` is the live manifest as a dumped dict (``tai42_app.admin.live_manifest``);
    a missing or malformed ``connectors`` key contributes nothing."""
    keys = set(env_secret_marks_settings().secret_keys)
    connectors = manifest.get("connectors")
    if isinstance(connectors, list):
        for connector in connectors:
            if isinstance(connector, dict):
                var = connector.get("client_secret_env")
                if isinstance(var, str) and var:
                    keys.add(var)
    return tuple(sorted(keys))
