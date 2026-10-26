"""Every worker judges ownership on one index: the fleet's deployment names and every worker's env readers.

A worker publishes the names its deployment env sets and a description of every settings name it
reads (the registered settings groups and the declared readers, such as FastMCP's settings). A
worker that does not load a reader another worker loads still owns every name that reader reads
for a field the deployment sets, so the two give the same answer for every name.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Iterator, Sequence

import fastmcp
import pytest
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from tai42_kit.settings import (
    EnvReader,
    TaiBaseSettings,
    declare_env_reader,
    decode_env_readers,
    describe_env_reader,
    encode_env_readers,
    registered_settings,
)
from tai42_kit.settings import reader_descriptions as env_readers_mod
from tai42_kit.settings import registry as settings_registry

from tai42_skeleton.app import applied_env
from tai42_skeleton.app.boot_rules import ConfigInvariantError
from tai42_skeleton.app.bus import WorkerKind, WorkerRow
from tai42_skeleton.app.fastmcp_settings import apply_fastmcp_settings
from tai42_skeleton.app.lifecycle.bus_subscription import _set_by_withdrawn
from tai42_skeleton.config.deployment_env import (
    DeploymentOwnership,
    fleet_deployment_ownership,
    published_deployment_keys,
    published_env_readers,
)

from .._fakes.bus import FakeBus, _row
from .._helpers import recapture_deployment_env
from .fake_pipeline import _reset_settings_after  # noqa: F401  — autouse fixture, active on import

_PREFIXES = ("FLEETRD_", "FASTMCP_DOCKET")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.upper().startswith(_PREFIXES):
            monkeypatch.delenv(key)
    monkeypatch.setattr(applied_env, "_loaded_env_keys", set())
    yield
    for key in list(os.environ):
        if key.upper().startswith(_PREFIXES):
            del os.environ[key]


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Registrations and reader declarations a test makes are dropped at its end."""
    saved = dict(settings_registry._REGISTRY)
    declared = dict(env_readers_mod._DECLARED)
    yield
    settings_registry._REGISTRY.clear()
    settings_registry._REGISTRY.update(saved)
    env_readers_mod._DECLARED.clear()
    env_readers_mod._DECLARED.update(declared)


def _define_plugin_groups() -> tuple[type[BaseSettings], type[BaseSettings]]:
    class FleetReadersConn(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="FLEETRD_C_")
        url: str | None = None
        timeout: float = 5

    class FleetReadersPlugin(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="FLEETRD_A_", env_nested_delimiter="__")
        conn: FleetReadersConn = Field(default_factory=FleetReadersConn)

    return FleetReadersPlugin, FleetReadersConn


_UNIVERSE = [
    "FLEETRD_C_URL",
    "fleetrd_c_url",
    "FLEETRD_C_TIMEOUT",
    "FLEETRD_A_CONN",
    "fleetrd_a_conn",
    "FLEETRD_A_CONN__URL",
    "fleetrd_a_conn__url",
    "FLEETRD_A_CONN__TIMEOUT",
    "FLEETRD_A_CONN__NOPE",
    "FLEETRD_A_OTHER",
    "FLEETRD_UNREAD",
]


def _every_registered_name() -> list[str]:
    return sorted({name for info in registered_settings() for field in info.fields for name in field.accepted_env_vars})


def _row_readers() -> dict[str, tuple[EnvReader, ...]]:
    own = published_env_readers()
    return {own.digest: decode_env_readers(own.body)}


@pytest.mark.parametrize(
    "deployment",
    [
        {"FLEETRD_C_URL": "redis://deployment"},
        {"FLEETRD_A_CONN": json.dumps({"url": "redis://deployment"})},
        {"FLEETRD_A_CONN__URL": "redis://deployment"},
        {"fleetrd_a_conn__timeout": "9"},
    ],
)
def test_a_worker_without_a_plugin_s_settings_owns_what_the_worker_with_them_owns(
    monkeypatch: pytest.MonkeyPatch, isolated_registry: None, deployment: dict[str, str]
) -> None:
    plugin, conn = _define_plugin_groups()
    for key, value in deployment.items():
        monkeypatch.setenv(key, value)
    recapture_deployment_env()
    universe = _UNIVERSE + _every_registered_name()
    written = dict.fromkeys(universe, "stored")
    with_plugin = DeploymentOwnership()
    a_owns = with_plugin.owned(universe)
    a_refuses = with_plugin.changed_writes(written, {})
    a_keys, a_readers = published_deployment_keys(), _row_readers()

    for cls in (plugin, conn):
        del settings_registry._REGISTRY[f"{cls.__module__}.{cls.__qualname__}"]
    for key in deployment:
        monkeypatch.delenv(key)
    recapture_deployment_env()
    without_plugin = DeploymentOwnership(a_keys, a_readers)

    assert a_owns
    assert without_plugin.owned(universe) == a_owns
    assert without_plugin.changed_writes(written, {}) == a_refuses


def test_a_worker_publishes_the_names_its_deployment_sets_as_they_are(
    monkeypatch: pytest.MonkeyPatch, isolated_registry: None
) -> None:
    _define_plugin_groups()
    monkeypatch.setenv("FLEETRD_C_URL", "redis://deployment")
    recapture_deployment_env()

    assert [key for key in published_deployment_keys() if key.startswith("FLEETRD_")] == ["FLEETRD_C_URL"]


@pytest.fixture
def fastmcp_docket_restored() -> Iterator[None]:
    saved = fastmcp.settings.docket
    yield
    fastmcp.settings.set_setting("docket", saved)


def test_a_fastmcp_setting_the_deployment_sets_is_owned_under_every_name_fastmcp_reads(
    monkeypatch: pytest.MonkeyPatch, fastmcp_docket_restored: None
) -> None:
    monkeypatch.setenv("FASTMCP_DOCKET_URL", "redis://deployment")
    recapture_deployment_env()
    ownership = DeploymentOwnership()
    other_names = ["FASTMCP_DOCKET", "FASTMCP_DOCKET__URL", "fastmcp_docket__url"]
    written = {
        "FASTMCP_DOCKET__URL": "redis://stored",
        "fastmcp_docket__url": "redis://stored",
        "FASTMCP_DOCKET": json.dumps({"url": "redis://stored"}),
    }

    assert ownership.owned(other_names) == sorted(other_names)
    assert ownership.changed_writes(written, {}) == sorted(written)
    assert not ownership.owns("FASTMCP_DOCKET__CONCURRENCY")

    applied_env._apply_env({"FASTMCP_DOCKET__URL": "redis://stored"}, ownership)
    apply_fastmcp_settings()

    assert "FASTMCP_DOCKET__URL" not in os.environ
    assert fastmcp.settings.docket.url == "redis://deployment"


class _ReadersBus(FakeBus):
    """A bus whose sibling row names ``digest`` and that holds the reader bodies in ``bodies``."""

    def __init__(self, digest: str, bodies: dict[str, tuple[EnvReader, ...]]) -> None:
        super().__init__(origin="serve-test", remotes=["backend-1"])
        self.rows[1].env_readers = digest
        self.reader_bodies = bodies


class _SiblingReader(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEETRD_S_", env_nested_delimiter="__")
    level: str = "info"
    extra: dict[str, str] = Field(default_factory=dict)


def _sibling_readers() -> tuple[str, dict[str, tuple[EnvReader, ...]]]:
    readers = (describe_env_reader(_SiblingReader),)
    return "f" * 64, {"f" * 64: readers}


async def test_the_fleet_read_fetches_a_reader_body_once_and_judges_by_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEETRD_S_LEVEL", "debug")
    recapture_deployment_env()
    digest, bodies = _sibling_readers()
    bus = _ReadersBus(digest, bodies)
    bus.rows[1].deployment_keys = []

    first = await fleet_deployment_ownership(bus)
    second = await fleet_deployment_ownership(bus)

    assert first.owns("fleetrd_s_level")
    assert not first.owns("FLEETRD_S_EXTRA__A")
    assert second.owns("FLEETRD_S_LEVEL")
    assert bus.env_reader_calls == [[digest]]


async def test_a_name_only_a_sibling_s_reader_reads_is_owned_through_the_sibling_s_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recapture_deployment_env()
    digest, bodies = _sibling_readers()
    bus = _ReadersBus(digest, bodies)
    bus.rows[1].deployment_keys = ["FLEETRD_S_EXTRA"]

    ownership = await fleet_deployment_ownership(bus)

    assert ownership.owns("FLEETRD_S_EXTRA__KEY")
    assert not ownership.owns("FLEETRD_S_LEVEL")


async def test_a_live_row_whose_reader_body_is_missing_raises_naming_the_worker_and_digest() -> None:
    recapture_deployment_env()
    bus = _ReadersBus("e" * 64, {})

    with pytest.raises(ConfigInvariantError, match=f"backend-1.*{'e' * 64}"):
        await fleet_deployment_ownership(bus)


def test_a_settings_group_registered_after_the_capture_changes_the_published_readers(isolated_registry: None) -> None:
    recapture_deployment_env()
    before = published_env_readers().digest

    _define_plugin_groups()

    after = published_env_readers()
    assert after.digest != before
    assert any(reader.qualname.endswith("FleetReadersPlugin") for reader in decode_env_readers(after.body))


def test_a_reader_declared_after_the_capture_changes_the_published_readers(isolated_registry: None) -> None:
    recapture_deployment_env()
    before = published_env_readers().digest

    class FleetReadersDeclared(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="FLEETRD_D_")
        level: str = "info"

    declare_env_reader(FleetReadersDeclared)

    after = published_env_readers()
    assert after.digest != before
    assert any(reader.qualname.endswith("FleetReadersDeclared") for reader in decode_env_readers(after.body))


def test_a_withdrawn_name_sets_the_recycle_keys_a_sibling_s_reader_reads_for_its_field() -> None:
    recapture_deployment_env()
    _digest, bodies = _sibling_readers()
    fleet = DeploymentOwnership(["FLEETRD_S_EXTRA"], bodies)

    awaiting = ["FLEETRD_S_EXTRA__KEY", "FLEETRD_S_LEVEL"]

    assert _set_by_withdrawn(awaiting, ["FLEETRD_S_EXTRA"], fleet) == ["FLEETRD_S_EXTRA__KEY"]
    assert _set_by_withdrawn(awaiting, ["FLEETRD_S_EXTRA"], DeploymentOwnership(["FLEETRD_S_EXTRA"])) == []


class _OldReader(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEETRD_OLD_")
    url: str | None = None


class _NewReader(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEETRD_NEW_")
    url: str | None = None


class _RollingBus:
    """A bus whose censuses answer in turn from ``censuses``; a read of ``held_back`` waits for ``release``."""

    def __init__(self, bodies: dict[str, tuple[EnvReader, ...]], held_back: str) -> None:
        self.bodies = bodies
        self.held_back = held_back
        self.release = asyncio.Event()
        self.censuses: list[list[WorkerRow]] = []

    async def census(self) -> list[WorkerRow]:
        return self.censuses.pop(0)

    async def env_readers(self, digests: Sequence[str]) -> dict[str, tuple[EnvReader, ...]]:
        if self.held_back in digests:
            await self.release.wait()
        return {digest: self.bodies[digest] for digest in digests if digest in self.bodies}


def _described(cls: type[BaseSettings]) -> tuple[str, tuple[EnvReader, ...]]:
    readers = (describe_env_reader(cls),)
    return hashlib.sha256(encode_env_readers(readers).encode()).hexdigest(), readers


def _reading_row(name: str, digest: str) -> WorkerRow:
    row = _row(name, WorkerKind.serve, pid=1, generation=1, pttl_ms=15000)
    row.env_readers = digest
    return row


async def test_a_read_answers_by_the_bodies_its_census_needs_while_another_read_forgets_them() -> None:
    recapture_deployment_env()
    own = published_env_readers().digest
    old, old_readers = _described(_OldReader)
    new, new_readers = _described(_NewReader)
    bus = _RollingBus({old: old_readers, new: new_readers}, held_back=new)
    bus.censuses = [[_reading_row("serve-1", own), _reading_row("serve-2", old)]]
    await fleet_deployment_ownership(bus)

    # A rolling update: the first read still sees the leaving worker beside the joining one and
    # waits on the joiner's body; the second read sees only this worker and forgets the others.
    bus.censuses = [
        [_reading_row("serve-1", own), _reading_row("serve-2", old), _reading_row("serve-3", new)],
        [_reading_row("serve-1", own)],
    ]
    rolling = asyncio.create_task(fleet_deployment_ownership(bus))
    await asyncio.sleep(0)
    settled = await fleet_deployment_ownership(bus)
    bus.release.set()
    ownership = await rolling

    assert settled.fields_set_by("FLEETRD_OLD_URL") == frozenset()
    assert ownership.fields_set_by("FLEETRD_OLD_URL")
    assert ownership.fields_set_by("FLEETRD_NEW_URL")
