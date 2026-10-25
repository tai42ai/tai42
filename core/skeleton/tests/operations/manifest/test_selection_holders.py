"""The held-tool check on an MCP selection change, through every write door.

A write that would stop picking a tool something still uses is refused with a 409 naming
the holders, and a write the check cannot decide because the server did not list its tools
is refused with a 502; either way nothing is written. The check reads the stored manifest,
the stored presets' active bodies, the registered rename referees and the server's own
listing (scripted here on the reload admin), never what the answering worker has bound.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
import yaml
from tai42_contract.presets import PresetBody
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.backup.sections import _import_env, _import_manifest
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import BadRequestError, ConflictError, UpstreamError
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.operations import manifest as manifest_ops
from tai42_skeleton.operations.manifest.selection_holders import refuse_held_unpicks, unpick_holders
from tai42_skeleton.operations.reference_graph import StoredPresets

from ..._fakes.bus import FakeBus
from ...config.fake_pipeline import FakeConfigStore, FakeReloadAdmin, _service, _with_bus

_URL = "https://example.com/mcp"
_TAIL = "remove those references first, or keep those tools picked"
_NOTIFY_MESSAGE = (
    "tool selection of 'acme' cannot unpick 'acme_send' — held by:\n- preset 'notify', which uses 'acme_send'\n" + _TAIL
)


def _entry(title: str = "acme", *, include: list[str] | None = None, exclude: list[str] | None = None) -> dict:
    return {
        "title": title,
        "config": {"type": "streamable_http", "url": _URL},
        "include": include or [],
        "exclude": exclude or [],
    }


def _holders(error: ConflictError) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", error.extra["holders"])


def _body(base_tool: str) -> PresetBody:
    return PresetBody(base_tool=base_tool, description="d", fixed_kwargs={}, extensions=[])


class _World:
    """The stored manifest, env, preset bodies and server listing one test writes against."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, stored_bodies: bool = True) -> None:
        self._monkeypatch = monkeypatch
        self.store = FakeConfigStore(manifest={"mcp": [_entry()]})
        self.bodies: dict[str, PresetBody] = {}
        self.served: dict[str, list[str]] = {"acme": ["acme_send", "acme_read"]}
        self.listing_error: Exception | None = None
        self.admin = FakeReloadAdmin(served=self.served)
        self.bus = FakeBus(origin="serve-test")
        _with_bus(monkeypatch)
        monkeypatch.setattr(instance.app, "_bus", self.bus)
        world = self

        async def list_active_bodies(_facet: Any) -> dict[str, PresetBody]:
            return dict(world.bodies)

        if stored_bodies:
            monkeypatch.setattr(type(instance.app.presets), "list_active_bodies", list_active_bodies)
        monkeypatch.setattr(ConfigService, "from_app", classmethod(lambda cls: world.service()))

    def service(self) -> ConfigService:
        self.admin._raise_listing = self.listing_error
        service, _admin, _bus = _service(self.store, admin=self.admin, bus=self.bus)
        return service

    def stored(self, title: str = "acme") -> dict:
        return next(entry for entry in self.store.manifest["mcp"] if entry["title"] == title)

    def referee(self, tool: str, kind: str, name: str) -> None:
        async def referee(candidate: str) -> list[ReferenceHolder]:
            if candidate != tool:
                return []
            return [ReferenceHolder(kind=kind, name=name, summary=f"{kind} {name!r}")]

        instance.app.tools.register_rename_referee(referee)


_WORKERS = ("bound", "failed", "deregistered")


@pytest.fixture(params=_WORKERS)
def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    """The world as seen from a worker where ``acme`` is bound, failed, or deregistered.

    The check reads nothing the answering worker bound for ``acme``, so every verdict is the
    same on each of the three.
    """
    if request.param == "bound":
        monkeypatch.setitem(instance.app._mcp_bound_tools, "acme", {"acme_send", "acme_read"})
    elif request.param == "failed":
        monkeypatch.setitem(instance.app._failed_mcps, "acme", {"error": "connection refused"})
    instance.app._rename_referee_registry.reset()
    yield _World(monkeypatch)
    instance.app._rename_referee_registry.reset()


# -- the write doors -------------------------------------------------------------------------

Door = Callable[[_World, dict], Awaitable[Any]]


async def _tool_filter(world: _World, entry: dict) -> Any:
    # The op the HTTP route and the MCP edge both call.
    return await manifest_ops.set_mcp_tool_filter(entry["title"], entry["include"], entry["exclude"])


async def _set_mcp_config(world: _World, entry: dict) -> Any:
    others = [row for row in world.store.manifest["mcp"] if row["title"] != entry["title"]]
    return await manifest_ops.set_mcp_config([*others, entry])


async def _add_entries_replace(world: _World, entry: dict) -> Any:
    return await manifest_ops.add_mcp_entries([entry], replace=True)


async def _update_manifest(world: _World, entry: dict) -> Any:
    document = copy.deepcopy(world.store.manifest)
    document["mcp"] = [entry if row["title"] == entry["title"] else row for row in document["mcp"]]
    return await manifest_ops.update_manifest(yaml.safe_dump(document))


async def _env_and_change(world: _World, entry: dict) -> Any:
    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        def mutator(document: dict[str, Any]) -> None:
            document["mcp"] = [entry if row["title"] == entry["title"] else row for row in document["mcp"]]

        return {"PICK_NOTE": "x"}, mutator

    return await ConfigService.from_app().apply_env_and_change(prepare)


async def _backup_manifest(world: _World, entry: dict) -> Any:
    document = copy.deepcopy(world.store.manifest)
    document["mcp"] = [entry if row["title"] == entry["title"] else row for row in document["mcp"]]
    return await _import_manifest(document)


DOORS: list[Door] = [
    _tool_filter,
    _set_mcp_config,
    _add_entries_replace,
    _update_manifest,
    _env_and_change,
    _backup_manifest,
]


# The doors that call the config service directly rather than through an operation.
_SERVICE_DOORS: frozenset[Door] = frozenset({_env_and_change, _backup_manifest})


@pytest.fixture(params=DOORS, ids=lambda door: door.__name__.lstrip("_"))
def door(request: pytest.FixtureRequest) -> Door:
    return request.param


# -- the refusal, on every door -------------------------------------------------------------


@pytest.mark.parametrize("change", [{"include": ["acme_read"]}, {"exclude": ["acme_send"]}])
async def test_every_door_refuses_an_unpick_a_preset_holds(world: _World, door: Door, change: dict) -> None:
    world.bodies = {"notify": _body("acme_send")}
    before = copy.deepcopy(world.store.manifest)

    with pytest.raises(ConflictError) as caught:
        await door(world, _entry(**change))

    assert caught.value.message == _NOTIFY_MESSAGE
    assert caught.value.status == 409
    assert [(h["kind"], h["name"]) for h in _holders(caught.value)] == [("preset", "notify")]
    assert world.store.manifest == before
    assert world.store.persisted == []


async def test_every_door_refuses_502_when_the_server_cannot_be_listed(world: _World, door: Door) -> None:
    world.listing_error = ConnectionError("refused at https://alice:hunter2@mcp.example/mcp?token=abc")
    before = copy.deepcopy(world.store.manifest)

    with pytest.raises(UpstreamError) as caught:
        await door(world, _entry(include=["acme_read"]))

    message = caught.value.message
    assert message.startswith("tool selection of 'acme' cannot be checked: the server did not list its tools: ")
    assert "hunter2" not in message
    assert "token=abc" not in message
    assert caught.value.status == 502
    assert world.store.manifest == before


async def test_every_door_refuses_an_invalid_write_400_before_listing_the_server(world: _World, door: Door) -> None:
    # The write is invalid (an unset ``!ENV`` marker) and also unpicks a held tool from a
    # server whose listing fails: the invalid document is refused first, with no listing.
    # The doors that call the config service directly see its ``ValueError``, which an
    # operation answers as a 400.
    world.bodies = {"notify": _body("acme_send")}
    world.listing_error = ConnectionError("refused")
    before = copy.deepcopy(world.store.manifest)
    refusal = ValueError if door in _SERVICE_DOORS else BadRequestError

    with pytest.raises(refusal, match="ACME_UNSET_PICK"):
        await door(world, _entry(include=["acme_read", "!ENV ${ACME_UNSET_PICK}"]))

    assert world.admin.listed == []
    assert world.store.manifest == before


async def test_every_door_writes_an_unpick_nothing_holds(world: _World, door: Door) -> None:
    world.store.manifest = {"mcp": [_entry(include=["acme_send", "acme_read"])]}

    await door(world, _entry(include=["acme_send"]))

    assert world.stored()["include"] == ["acme_send"]
    assert world.admin.listed == []


async def test_a_registered_rename_referee_refuses_the_unpick(world: _World) -> None:
    world.referee("acme_send", "schedule", "nightly")

    with pytest.raises(ConflictError) as caught:
        await _tool_filter(world, _entry(include=["acme_read"]))

    assert caught.value.message == (
        "tool selection of 'acme' cannot unpick 'acme_send' — held by:\n"
        "- schedule 'nightly', which uses 'acme_send'\n" + _TAIL
    )
    assert world.stored()["include"] == []


async def test_a_raising_referee_propagates_and_nothing_is_written(world: _World) -> None:
    async def boom(_tool: str) -> list[ReferenceHolder]:
        raise RuntimeError("referee store unreadable")

    instance.app.tools.register_rename_referee(boom)

    with pytest.raises(RuntimeError, match="referee store unreadable"):
        await _tool_filter(world, _entry(include=["acme_read"]))
    assert world.store.persisted == []


async def test_the_states_referee_refuses_a_tool_held_only_by_state_records(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.states import db as states_db
    from tai42_skeleton.states.store import PostgresStatesStore
    from tai42_skeleton.tools import platform_referees

    async def count(_store: Any, kind: str, name: str) -> int:
        return 3 if (kind, name) == ("tool", "acme_send") else 0

    async def outstanding(_store: Any, _key: str) -> list:
        return []

    monkeypatch.setattr(states_db, "states_store_configured", lambda: True)
    monkeypatch.setattr(PostgresStatesStore, "__init__", lambda self: None)
    monkeypatch.setattr(PostgresStatesStore, "count_records_for_target", count)
    monkeypatch.setattr(PostgresStatesStore, "outbox_outstanding_on_target", outstanding)
    instance.app.tools.register_rename_referee(platform_referees._states_referee)

    with pytest.raises(ConflictError) as caught:
        await _tool_filter(world, _entry(include=["acme_read"]))

    assert caught.value.message == (
        "tool selection of 'acme' cannot unpick 'acme_send' — held by:\n"
        "- 3 state records under target tool/acme_send, which uses 'acme_send'\n" + _TAIL
    )
    assert world.stored()["include"] == []


async def test_the_tool_extensions_map_refuses_an_unpick_of_an_extended_tool(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.tools import platform_referees

    extended = {**_entry(), "extensions": {"acme_send": [["ext"]]}}
    world.store.manifest = {"mcp": [extended]}
    world.bodies = {"notify_ext": _body("acme_send_ext")}
    live = Manifest.model_validate({"mcp": [extended]})
    monkeypatch.setattr(instance.app, "_require_live_manifest", lambda: live)
    instance.app.tools.register_rename_referee(platform_referees._tool_extensions_referee)

    with pytest.raises(ConflictError) as caught:
        await _tool_filter(world, {**extended, "include": ["acme_read"]})

    assert ("tool extensions", "acme_send") in {(h["kind"], h["name"]) for h in _holders(caught.value)}
    assert world.stored()["include"] == []


async def test_removing_the_whole_entry_is_not_an_unpick(world: _World) -> None:
    world.bodies = {"notify": _body("acme_send")}
    world.listing_error = TimeoutError("down")

    await manifest_ops.remove_mcp_entry("acme")

    assert world.store.manifest["mcp"] == []
    assert world.admin.listed == []


async def test_a_backup_restore_reports_the_refused_manifest_section_and_applies_the_others(world: _World) -> None:
    from tai42_skeleton.operations import backup as backup_ops

    world.bodies = {"notify": _body("acme_send")}
    document = {
        "version": 1,
        "sections": {"manifest": {"mcp": [_entry(include=["acme_read"])]}, "env": {"OTHER_KEY": "1"}},
    }

    response = await backup_ops.import_backup(document, ["manifest", "env"])

    result = response.payload
    assert result.ok is False
    assert any("cannot unpick 'acme_send'" in str(error) for error in result.sections["manifest"].errors)
    assert result.sections["env"].errors == []
    assert world.stored()["include"] == []
    assert world.store.env["OTHER_KEY"] == "1"


# -- leaving names ---------------------------------------------------------------------------


async def test_from_every_tool_to_an_include_list_finds_a_served_tool_no_list_names(world: _World) -> None:
    world.served["acme"] = ["acme_send", "acme_read", "acme_write"]
    world.bodies = {"notify": _body("acme_send")}
    world.referee("acme_write", "schedule", "nightly")

    with pytest.raises(ConflictError) as caught:
        await _tool_filter(world, _entry(include=["acme_read"]))

    assert caught.value.message == (
        "tool selection of 'acme' cannot unpick 'acme_send', 'acme_write' — held by:\n"
        "- preset 'notify', which uses 'acme_send'\n"
        "- schedule 'nightly', which uses 'acme_write'\n" + _TAIL
    )
    assert world.admin.listed == ["acme"]


async def test_from_an_exclude_list_to_an_include_list_finds_every_served_leaving_name(world: _World) -> None:
    world.served["acme"] = ["acme_send", "acme_read", "acme_write"]
    world.store.manifest = {"mcp": [_entry(exclude=["acme_read"])]}
    world.bodies = {"notify": _body("acme_send")}
    world.referee("acme_write", "schedule", "nightly")

    with pytest.raises(ConflictError) as caught:
        await _tool_filter(world, _entry(include=["acme_read"]))

    assert "cannot unpick 'acme_send', 'acme_write'" in caught.value.message
    assert {h["name"] for h in _holders(caught.value)} == {"notify", "nightly"}

    world.listing_error = TimeoutError("down")
    with pytest.raises(UpstreamError):
        await _tool_filter(world, _entry(include=["acme_read"]))
    assert world.stored()["exclude"] == ["acme_read"]


async def test_from_every_tool_with_nothing_held_still_needs_the_listing(world: _World) -> None:
    world.listing_error = TimeoutError("down")

    with pytest.raises(UpstreamError, match="tool selection of 'acme' cannot be checked"):
        await _tool_filter(world, _entry(include=["acme_send", "acme_read"]))
    assert world.stored()["include"] == []


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param({"include": ["acme_read"]}, {}, id="all"),
        pytest.param({"include": ["acme_read"]}, {"include": ["acme_read", "acme_send"]}, id="widened"),
        pytest.param({"include": ["acme_send", "acme_read"]}, {"include": ["acme_send"]}, id="unheld-narrowing"),
    ],
)
async def test_a_change_with_no_held_leaving_name_passes_without_a_listing(
    world: _World, before: dict, after: dict
) -> None:
    world.bodies = {"notify": _body("acme_send")}
    world.store.manifest = {"mcp": [_entry(**before)]}
    world.listing_error = TimeoutError("down")

    await _tool_filter(world, _entry(**after))

    assert world.stored()["include"] == after.get("include", [])


async def test_unpicking_an_exclude_named_tool_needs_the_listing(world: _World) -> None:
    world.bodies = {"notify": _body("acme_send")}
    world.listing_error = TimeoutError("down")

    with pytest.raises(UpstreamError):
        await _tool_filter(world, _entry(exclude=["acme_send"]))


# -- held names the listing does not serve -----------------------------------------------


async def test_a_stale_name_the_server_no_longer_serves_passes(world: _World) -> None:
    world.store.manifest = {"mcp": [_entry(include=["acme_send", "acme_old"])]}
    world.bodies = {"notify2": _body("acme_old")}

    await _tool_filter(world, _entry(include=["acme_send"]))

    assert world.stored()["include"] == ["acme_send"]


async def test_another_titles_name_passes_unless_this_server_also_serves_it(world: _World) -> None:
    world.store.manifest = {
        "mcp": [_entry(include=["acme_send", "acme_mail_send"]), _entry("acme_mail")],
    }
    world.served["acme_mail"] = ["acme_mail_send"]
    world.bodies = {"p": _body("acme_mail_send")}

    await _tool_filter(world, _entry(include=["acme_send"]))
    assert world.stored()["include"] == ["acme_send"]

    world.store.manifest["mcp"][0]["include"] = ["acme_send", "acme_mail_send"]
    world.served["acme"] = ["acme_send", "acme_mail_send"]
    with pytest.raises(ConflictError, match="cannot unpick 'acme_mail_send'") as caught:
        await _tool_filter(world, _entry(include=["acme_send"]))
    assert [h["name"] for h in _holders(caught.value)] == ["p"]


async def test_a_branch_name_the_server_does_not_serve_passes(world: _World) -> None:
    world.store.manifest = {"mcp": [{**_entry(include=["acme_send", "acme_send_ext"]), "extensions": {}}]}
    world.bodies = {"b": _body("acme_send_ext")}

    await _tool_filter(world, _entry(include=["acme_send"]))

    assert world.stored()["include"] == ["acme_send"]


async def test_a_prefixed_tool_outside_mcp_passes(world: _World) -> None:
    world.store.manifest = {"mcp": [_entry(include=["acme_send", "acme_report"])]}
    world.bodies = {"r": _body("acme_report")}

    await _tool_filter(world, _entry(include=["acme_send"]))

    assert world.stored()["include"] == ["acme_send"]


async def test_a_held_name_without_the_titles_prefix_is_never_a_leaving_name(world: _World) -> None:
    world.store.manifest = {"mcp": [_entry(include=["acme_send", "other_send"])]}
    world.bodies = {"o": _body("other_send")}
    world.listing_error = TimeoutError("down")

    await _tool_filter(world, _entry(include=["acme_send"]))

    assert world.stored()["include"] == ["acme_send"]
    assert world.admin.listed == []


async def test_an_unusable_schema_tool_the_binder_never_binds_passes(world: _World) -> None:
    # The listing serves only the names the binder can adapt: ``acme_blob`` is not one.
    world.store.manifest = {"mcp": [_entry(include=["acme_send", "acme_blob"])]}
    world.referee("acme_blob", "schedule", "nightly")

    await _tool_filter(world, _entry(include=["acme_send"]))
    assert world.stored()["include"] == ["acme_send"]

    world.store.manifest = {"mcp": [_entry(include=["acme_send", "acme_blob"])]}
    world.listing_error = TimeoutError("down")
    with pytest.raises(UpstreamError):
        await _tool_filter(world, _entry(include=["acme_send"]))


async def test_a_served_name_a_stored_preset_owns_passes(world: _World) -> None:
    world.served["acme"] = ["acme_send", "acme_read", "acme_ping"]
    world.bodies = {"acme_ping": _body("echo")}
    world.referee("acme_ping", "schedule", "nightly")

    await _tool_filter(world, _entry(include=["acme_send", "acme_read"]))

    assert world.stored()["include"] == ["acme_send", "acme_read"]


# -- doors that change the env under an ``!ENV`` selection marker ----------------------------


@pytest.fixture
def marker_world(world: _World, monkeypatch: pytest.MonkeyPatch) -> _World:
    monkeypatch.setenv("ACME_PICK", "acme_send")
    world.store.manifest = {"mcp": [_entry(include=["!ENV ${ACME_PICK}"])]}
    world.store.env = {"ACME_PICK": "acme_send"}
    world.bodies = {"notify": _body("acme_send")}
    return world


async def test_an_env_write_that_repoints_a_selection_marker_is_refused(marker_world: _World) -> None:
    with pytest.raises(ConflictError) as caught:
        await config_ops.write_env({"ACME_PICK": "acme_read"})

    assert caught.value.message == _NOTIFY_MESSAGE
    assert marker_world.store.env_writes == []


async def test_a_backup_env_restore_that_repoints_a_selection_marker_is_refused(marker_world: _World) -> None:
    with pytest.raises(ConflictError, match="cannot unpick 'acme_send'"):
        await _import_env({"ACME_PICK": "acme_read"})
    assert marker_world.store.env_writes == []


async def test_a_secret_marker_written_into_a_selection_list_is_refused(world: _World) -> None:
    world.store.manifest = {"mcp": [_entry(include=["acme_send"])]}
    world.bodies = {"notify": _body("acme_send")}

    with pytest.raises(ConflictError, match="cannot unpick 'acme_send'"):
        await manifest_ops.set_mcp_secret_env(value="acme_read", manifest_pointer="mcp/0/include/0", key="ACME_PICK")
    assert world.store.env_writes == []
    assert world.stored()["include"] == ["acme_send"]


async def test_a_profile_replace_that_repoints_a_selection_marker_is_refused(marker_world: _World) -> None:
    async def save_previous(_env: dict[str, str]) -> None:
        raise AssertionError("snapshotted before the held-tool check")

    with pytest.raises(ConflictError, match="cannot unpick 'acme_send'"):
        await marker_world.service().apply_replace_env({"ACME_PICK": "acme_read"}, save_previous=save_previous)
    assert marker_world.store.env_writes == []


async def test_an_env_write_that_keeps_the_selection_passes_without_a_listing(marker_world: _World) -> None:
    marker_world.listing_error = TimeoutError("down")

    await config_ops.write_env({"OTHER_KEY": "1"})

    assert marker_world.store.env["OTHER_KEY"] == "1"
    assert marker_world.admin.listed == []


# -- the declared errors -------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        manifest_ops.set_mcp_tool_filter,
        manifest_ops.set_mcp_config,
        manifest_ops.add_mcp_entries,
        manifest_ops.update_manifest,
        manifest_ops.set_mcp_secret_env,
        config_ops.write_env,
        config_ops.apply_profile,
    ],
)
def test_each_selection_write_door_declares_409_and_502(op: Any) -> None:
    from tai42_skeleton.operations import operation_metadata_of

    declared = set(operation_metadata_of(op).error_classes)
    assert {ConflictError, UpstreamError} <= declared


# -- the check itself ------------------------------------------------------------------------


async def test_unpick_holders_lists_a_preset_once_when_both_legs_name_it(world: _World) -> None:
    world.referee("acme_send", "preset", "notify")

    holders = await unpick_holders("acme_send", StoredPresets({"notify": _body("acme_send")}))

    assert [(h.kind, h.name, h.summary) for h in holders] == [
        ("preset", "notify", "preset 'notify', which uses 'acme_send'")
    ]


async def test_a_check_over_many_leaving_names_reads_and_walks_each_stored_body_once(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.operations import reference_graph

    world.served["acme"] = ["acme_send", "acme_read", "acme_a", "acme_b", "acme_c"]
    world.bodies = {"notify": _body("acme_send"), "n2": _body("echo"), "n3": _body("echo")}
    reads: list[None] = []
    walks: list[str] = []
    list_active_bodies = type(instance.app.presets).list_active_bodies
    composed_origins = reference_graph._composed_origins

    async def counted_read(facet: Any) -> dict[str, PresetBody]:
        reads.append(None)
        return await list_active_bodies(facet)

    async def counted_walk(name: str, body: PresetBody) -> set[str]:
        walks.append(name)
        return await composed_origins(name, body)

    monkeypatch.setattr(type(instance.app.presets), "list_active_bodies", counted_read)
    monkeypatch.setattr(reference_graph, "_composed_origins", counted_walk)
    before = Manifest.model_validate({"mcp": [_entry()]})
    after = Manifest.model_validate({"mcp": [_entry(include=["acme_read"])]})

    with pytest.raises(ConflictError) as caught:
        await refuse_held_unpicks(before, after, world.admin.served_mcp_names)

    assert caught.value.message == _NOTIFY_MESSAGE
    assert len(reads) == 1
    assert sorted(walks) == ["n2", "n3", "notify"]


async def test_refuse_held_unpicks_joins_every_refusing_title(world: _World) -> None:
    from tai42_skeleton.manifest import Manifest

    world.served["beta"] = ["beta_send"]
    world.bodies = {"notify": _body("acme_send"), "n2": _body("beta_send")}
    before = Manifest.model_validate({"mcp": [_entry(include=["acme_send"]), _entry("beta", include=["beta_send"])]})
    after = Manifest.model_validate({"mcp": [_entry(include=["acme_x"]), _entry("beta", include=["beta_x"])]})

    with pytest.raises(ConflictError) as caught:
        await refuse_held_unpicks(before, after, world.admin.served_mcp_names)

    assert caught.value.message.startswith(
        "tool selection of 'acme' cannot unpick 'acme_send'; tool selection of 'beta' cannot unpick 'beta_send' — "
    )


# -- the HTTP doors, end to end ------------------------------------------------------------


def _http_requests(entry: dict, document: dict) -> list[tuple[str, str, dict]]:
    return [
        ("PUT", "/api/mcp-config/entries/acme/tool-filter", {"include": entry["include"], "exclude": entry["exclude"]}),
        ("POST", "/api/mcp-config", {"mcp": [entry]}),
        ("POST", "/api/mcp-config/entries", {"entries": [entry], "replace": True}),
        ("POST", "/api/manifest/replace", {"manifest_text": yaml.safe_dump({**document, "mcp": [entry]})}),
    ]


@pytest.mark.parametrize("door_index", range(4), ids=["tool-filter", "mcp-config", "entries-replace", "replace"])
def test_each_http_door_answers_409_with_the_holders(world: _World, door_index: int) -> None:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from tai42_skeleton.routers import manifest as router

    world.bodies = {"notify": _body("acme_send")}
    app = Starlette(
        routes=[
            Route("/api/mcp-config/entries/{title}/tool-filter", router.set_mcp_tool_filter, methods=["PUT"]),
            Route("/api/mcp-config", router.set_mcp_config, methods=["POST"]),
            Route("/api/mcp-config/entries", router.add_mcp_entries, methods=["POST"]),
            Route("/api/manifest/replace", router.update_manifest, methods=["POST"]),
        ]
    )
    method, path, body = _http_requests(_entry(include=["acme_read"]), world.store.manifest)[door_index]
    before = copy.deepcopy(world.store.manifest)

    response = TestClient(app).request(method, path, json=body)

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"] == _NOTIFY_MESSAGE
    assert [(h["kind"], h["name"]) for h in payload["holders"]] == [("preset", "notify")]
    assert world.store.manifest == before


# -- no skeleton database ------------------------------------------------------------------


@pytest.fixture
def store_less_world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    """No skeleton database: the stored presets read empty and opening the versioned store fails the test."""
    import tai42_skeleton.versioning.store as store_module

    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    @asynccontextmanager
    async def forbid_client_ctx(client_cls: Any, settings: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        raise AssertionError("versioned store opened with no skeleton database")
        yield  # pragma: no cover - unreachable, satisfies the context-manager protocol

    monkeypatch.setattr(store_module, "client_ctx", forbid_client_ctx)
    instance.app._rename_referee_registry.reset()
    world = _World(monkeypatch, stored_bodies=False)
    world.store.manifest = {"mcp": [_entry(include=["acme_send", "acme_read"])]}
    yield world
    instance.app._rename_referee_registry.reset()


async def test_with_no_skeleton_database_an_unheld_unpick_is_written(store_less_world: _World) -> None:
    await _tool_filter(store_less_world, _entry(include=["acme_read"]))

    assert store_less_world.stored()["include"] == ["acme_read"]


async def test_with_no_skeleton_database_a_referee_still_refuses(store_less_world: _World) -> None:
    store_less_world.referee("acme_send", "schedule", "nightly")

    with pytest.raises(ConflictError) as caught:
        await _tool_filter(store_less_world, _entry(include=["acme_read"]))

    assert [h["name"] for h in _holders(caught.value)] == ["nightly"]
    assert store_less_world.stored()["include"] == ["acme_send", "acme_read"]
