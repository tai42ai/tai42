"""Response models for the config operations: env, mode, settings schema and settings profiles."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, JsonValue, RootModel
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_kit.settings import SettingsFieldInfo


class EnvView(BaseModel):
    """The stored env map plus the operator's masked-key marks and the stored keys the deployment sets.

    ``env`` values are verbatim (masking is display-side, never on the wire); ``secret_keys`` names which
    keys the UI masks; ``deployment_keys`` names the stored keys the deployment sets, which a write may
    remove or carry unchanged but never set to a new value.
    """

    env: dict[str, str]
    secret_keys: list[str]
    deployment_keys: list[str]


class ConfigModeView(BaseModel):
    """The active config backend mode: ``file`` (built in) or an external provider's mode name."""

    config_mode: str


class SettingsFieldView(SettingsFieldInfo):
    """One settings field: its registered metadata, the value it resolves to, and the layer that supplied it.

    ``value_source``: ``"deployment"`` (the deployment env sets the field; it is locked, and a save that
    sets it is refused), ``"stored"`` (the stored env), ``"default_namespace"`` (a ``TAI_DEFAULT_*``
    fallback), ``"field_default"`` (the bare field default), or ``null`` for a nested-group reference,
    which carries no value of its own.
    """

    value: JsonValue
    value_source: Literal["deployment", "stored", "default_namespace", "field_default"] | None


class SettingsGroupView(BaseModel):
    """One registered settings group with each field's resolved value."""

    name: str
    module: str
    qualname: str
    fields: list[SettingsFieldView]


class SettingsSchemaView(BaseModel):
    """Every registered settings group with its per-field resolved values."""

    groups: list[SettingsGroupView]


class ProfileSummary(BaseModel):
    """One settings-profile listing row."""

    name: str
    description: str


class ProfileListResponse(RootModel[list[ProfileSummary]]):
    """The bare array of settings-profile listing rows."""


class ProfileWriteResult(BaseModel):
    """A profile write/rollback confirmation — the new active version, never the body.

    So a secret never re-emits on the write path.
    """

    ok: bool
    version: int


class OkResult(BaseModel):
    """A bare ``{ok}`` confirmation."""

    ok: bool


class ProfileDiffChange(BaseModel):
    """One changed env key in a profile diff.

    ``old``/``new`` are the env VALUES on each side (real values — the UI masks); NEVER widened where rendered.
    """

    key: str
    old: str
    new: str


class ProfileDiff(BaseModel):
    """A settings-profile-vs-stored-env diff.

    ``added``/``removed``/``recycle_keys``/``refused_keys`` are key names; ``changed`` carries the per-key value change.
    """

    added: list[str]
    removed: list[str]
    changed: list[ProfileDiffChange]
    recycle_keys: list[str]
    refused_keys: list[str]


class ProfileVersionSummary(BaseModel):
    """One row of a settings-profile version history.

    ``created_at`` is an ISO-8601 timestamp string; ``is_current`` marks the active version.
    """

    version: int
    tags: list[str]
    created_at: str
    is_current: bool


class ProfileVersionListResponse(RootModel[list[ProfileVersionSummary]]):
    """The bare array of settings-profile version-history rows."""


class ProfileVersionView(BaseModel):
    """One settings-profile version row extended with its full ``body``.

    Real env values — this door is secret-fenced; masking is display-side only.
    """

    version: int
    tags: list[str]
    created_at: str
    is_current: bool
    body: SettingsProfileBody
