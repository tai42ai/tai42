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

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Final

from pydantic import Field, field_validator
from pydantic_settings import NoDecode
from tai42_kit.settings import TaiBaseSettings, settings_cache

from tai42_skeleton.connectors.manifest_env import connector_client_env_refs

# The env var holding the operator's marks: a comma-separated list of env key names.
SECRET_MARKS_ENV_VAR: Final = "TAI_ENV_SECRET_KEYS"  # noqa: S105 constant identifier, not a secret value


def parse_secret_marks(value: str | None) -> list[str]:
    """The marks variable's value as an ordered, de-duplicated list of trimmed names; empty segments dropped."""
    return list(dict.fromkeys(mark.strip() for mark in (value or "").split(",") if mark.strip()))


def format_secret_marks(marks: Iterable[str]) -> str:
    """The marks variable's value for ``marks``."""
    return ",".join(marks)


def merge_secret_marks(stored: str | None, added: Iterable[str]) -> str:
    """The marks variable's value for the ordered union of the ``stored`` value and ``added``."""
    return format_secret_marks(dict.fromkeys([*parse_secret_marks(stored), *added]))


class EnvSecretMarksSettings(TaiBaseSettings):
    """The operator's marks for which env keys to treat as secret."""

    # Names of env keys the operator marked secret. ``NoDecode`` disables
    # pydantic-settings' JSON decode for this complex field so the raw
    # comma-separated env string reaches the ``mode="before"`` validator, which
    # splits it into a list.
    secret_keys: Annotated[list[str], NoDecode] = Field(default_factory=list, validation_alias=SECRET_MARKS_ENV_VAR)

    @field_validator("secret_keys", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept the comma-separated env string through :func:`parse_secret_marks`.

        Env values are strings; a non-string value is passed through unchanged.
        """
        if isinstance(value, str):
            return parse_secret_marks(value)
        return value


@settings_cache
def env_secret_marks_settings() -> EnvSecretMarksSettings:
    """The cached operator secret-marks settings group."""
    return EnvSecretMarksSettings()


def effective_secret_keys(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """The env key names masked as secret, deduped and sorted.

    The stored operator marks (``EnvSecretMarksSettings.secret_keys``) UNIONED with
    every live oauth connector's ``client_secret_env``.
    Secret-ness of a connector's client secret is an invariant the manifest already
    STATES, so it is DERIVED here at read time rather than duplicated into the env
    store — an oauth connector's secret value stays masked even with no operator mark.
    ``manifest`` is the live manifest as a dumped dict (``tai42_app.admin.live_manifest``);
    a missing or malformed ``connectors`` key contributes nothing.
    """
    keys = set(env_secret_marks_settings().secret_keys)
    keys.update(ref.var for ref in connector_client_env_refs(manifest) if ref.secret)
    return tuple(sorted(keys))
