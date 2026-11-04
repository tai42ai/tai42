"""Response models for the config operations: env, mode, settings schema and settings profiles."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, JsonValue, RootModel
from tai42_contract.app.responses import AwaitingWorker, RecycleRoll
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_kit.settings import SettingsFieldInfo

from tai42_skeleton.app.bus import FleetResult


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


class FleetReloadResponse(FleetResult):
    """A fleet soft restart's per-worker reload report, the recycle roll it ran, and its verdict.

    An applied worker's ``payload`` is ``{status: "ok", env_keys, awaiting_respawn}``; a worker
    whose reload was refused at its epoch build (the build discarded before its swap) replies
    ``failed`` with ``{status: "discarded", awaiting_respawn}``, serving its previous epoch with
    that drift standing; any other failed reload, a failed discard among them, carries no payload.
    ``recycle`` is the roll that restarted the workers the reload addressed whose reload built and
    whose ``awaiting_respawn`` named a recycle-class key, or whose state was unknown: a worker other
    than the answering one is asked once ready and restarted when its reply neither confirms it
    clean nor states a refused build; the answering worker is never asked, and when its own state is
    unknown its restart is its ``self-deferred`` line, present when the roll converged. A worker
    whose reload was refused at its epoch build is never restarted: the stored config did not build
    on it and a restart may fail the same way, so the roll leaves it running and lists it.
    ``recycle`` is ``null`` when the roll had no worker to visit: every addressed worker confirmed
    no recycle-class key, or the only ones naming one were refused at their epoch build.
    ``awaiting_respawn`` the workers still awaiting a respawn after the roll (name + keys), those
    left running after a refused epoch build among them. ``roll_deferred`` is ``true`` when the roll
    was handed to the worker ``roll_deferred_to`` names, which carries it out after the reply
    (``recycle`` is ``null`` and ``awaiting_respawn`` empty); ``roll_deferred_to`` is ``null``
    otherwise.
    """

    recycle: RecycleRoll | None = None
    awaiting_respawn: list[AwaitingWorker] = Field(default_factory=list[AwaitingWorker])
    roll_deferred: bool = False
    roll_deferred_to: str | None = None


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
