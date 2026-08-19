"""Op-level oracles for the preset operations.

The route oracles in ``tests/routers/test_presets.py`` drive every door's happy and
common-error paths through the adapter; these pin the op-only branches those
round-trips do not reach — the pure body-structure readers, the residual /
typed-race error paths inside the mutating ops, and the destructive projection.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.manifest import ApiToolsConfig
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import (
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
)
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError, NotFoundError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.operations.projection import project_operations

# Importing the router registers the routes, which forces ``destructive`` on the
# DELETE op (the adapter's DELETE rule) so the projection oracle sees it.
from tai42_skeleton.routers import presets as _presets_router  # noqa: F401

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [
        {
            "title": "fx",
            "module": "tests.presets._fixtures",
            "include": ["weather", "echo", "plan_tool", "boom_tool"],
        }
    ],
    "agents": [
        {
            "title": "ag",
            "module": "tests.routers._authoring_fixtures",
            "include": ["authorable_agent", "locked_agent"],
        }
    ],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def pg(monkeypatch) -> FakeVersioningPg:
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    return fake


@pytest.fixture(autouse=True)
def _reset_preset_registry():
    """Tear down every runtime-registered / quarantined preset after each test —
    the singleton ``PresetManager`` outlives one ``app_context``."""
    yield
    mgr = instance.app.preset_manager

    async def _clear() -> None:
        for name in list(mgr.registered_names()):
            await mgr.remove(name)
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


async def _create(name: str, base_tool: str = "weather", **over: Any) -> None:
    # ``description`` is required non-empty on create, so the helper seeds a default
    # one; a test that exercises the description gate overrides it.
    await preset_ops.create_preset(
        name=name,
        base_tool=base_tool,
        description=over.get("description", "d"),
        fixed_kwargs=over.get("fixed_kwargs", {}),
        extensions=over.get("extensions", []),
        output_schema=over.get("output_schema"),
    )


# -- pure body-structure readers ---------------------------------------------


def test_read_element_rejects_empty_string() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="non-empty string"):
        preset_ops.read_element("")


def test_read_element_rejects_non_str_non_dict() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="extension name or a"):
        preset_ops.read_element(42)


def test_read_element_rejects_missing_name() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="non-empty string 'name'"):
        preset_ops.read_element({"config": {}})


def test_read_element_rejects_missing_config() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="must carry a 'config' mapping"):
        preset_ops.read_element({"name": "x"})


def test_read_element_rejects_unexpected_keys() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="unexpected keys"):
        preset_ops.read_element({"name": "x", "config": {}, "junk": 1})


def test_read_element_accepts_name_config() -> None:
    assert preset_ops.read_element({"name": "x", "config": {"a": 1}}) == {"name": "x", "config": {"a": 1}}


def test_read_combos_rejects_empty_inner_combo() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="non-empty list"):
        preset_ops.read_combos([[]])


def test_read_create_extensions_absent_is_empty() -> None:
    assert preset_ops.read_create_extensions(False, None) == []


def test_read_create_extensions_explicit_empty_rejected() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="explicit empty"):
        preset_ops.read_create_extensions(True, [])


def test_read_create_extensions_non_list_rejected() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="must be a list of combos"):
        preset_ops.read_create_extensions(True, "nope")


def test_read_edit_extensions_absent_or_null_carries() -> None:
    assert preset_ops.read_edit_extensions(False, None) is None
    assert preset_ops.read_edit_extensions(True, None) is None


def test_read_edit_extensions_empty_clears() -> None:
    assert preset_ops.read_edit_extensions(True, []) == []


def test_read_edit_extensions_non_list_rejected() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="must be a list of combos"):
        preset_ops.read_edit_extensions(True, 5)


def test_read_output_schema_variants() -> None:
    assert preset_ops.read_output_schema(None) is None
    assert preset_ops.read_output_schema({"type": "object"}) == {"type": "object"}
    with pytest.raises(preset_ops.BadRequestError, match="JSON object"):
        preset_ops.read_output_schema("nope")


def test_referenced_tool_names_builtin_walk() -> None:
    assert preset_ops._referenced_tool_names({"tool_names": ["other"]}) == {"other"}
    assert preset_ops._referenced_tool_names({"subagents": [{"tool_names": ["x"]}]}) == {"x"}
    assert preset_ops._referenced_tool_names({"tool_names": ["other"]}) & {"target"} == set()


# -- destructive projection --------------------------------------------------


def test_destructive_preset_ops_project_with_destructive_hint() -> None:
    """The mutating preset ops carry ``destructiveHint`` when projected; the reads
    and the dry-run validate do not. ``delete_preset`` gets its destructive flag from
    the adapter's DELETE rule (the router import above forced it)."""
    all_ops = (
        "list_presets",
        "create_preset",
        "get_preset",
        "list_versions",
        "get_version",
        "save_version",
        "rollback_preset",
        "rename_preset",
        "delete_preset",
        "preset_referees",
        "validate_preset",
        "set_preset_version_tags",
    )
    destructive = {
        "create_preset",
        "save_version",
        "rollback_preset",
        "rename_preset",
        "delete_preset",
        "set_preset_version_tags",
    }

    reg = OperationRegistry()
    for name in all_ops:
        op = operation_metadata_of(getattr(preset_ops, name))
        assert op.destructive is (op.name in destructive), f"{op.name} destructive={op.destructive}"
        reg.register(op)

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = annotations
            return lambda fn: fn

    class _App:
        def __init__(self) -> None:
            self.tools = _Rec()

    app = _App()
    project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    for name in destructive:
        annotations = app.tools.registered[name]
        assert annotations is not None
        assert annotations.destructiveHint is True
    for name in ("list_presets", "get_preset", "validate_preset"):
        assert app.tools.registered[name] is None


# -- create: typed store errors + residual register-failure branches ---------


def test_create_store_name_conflict_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(self, *a, **k):
                raise PresetNameConflictError("weather")

            # The store view is rebuilt per access, so the seam is patched on the class.
            monkeypatch.setattr(type(instance.app.presets.store), "create_preset", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_create_store_exists_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(self, *a, **k):
                raise PresetExistsError("p")

            monkeypatch.setattr(type(instance.app.presets.store), "create_preset", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_create_register_exists_race_rolls_back_and_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(*a, **k):
                raise PresetExistsError("p")

            monkeypatch.setattr(instance.app.preset_manager, "register", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await _create("p", fixed_kwargs={"units": "v"})
            # The store row was rolled back (HARD delete) — no stored-but-unregistered
            # preset survives.
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("p")

    asyncio.run(run())


def test_create_register_name_conflict_race_rolls_back_and_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(*a, **k):
                raise PresetNameConflictError("p")

            monkeypatch.setattr(instance.app.preset_manager, "register", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_create_rollback_delete_failure_reraises_delete_error(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def reg_boom(*a, **k):
                raise RuntimeError("register kaput")

            async def del_boom(*a, **k):
                raise RuntimeError("delete kaput")

            monkeypatch.setattr(instance.app.preset_manager, "register", reg_boom)
            monkeypatch.setattr(type(instance.app.versioning.store), "delete", del_boom)
            # The delete failure during rollback surfaces loudly (chained from the
            # register failure), never swallowed.
            with pytest.raises(RuntimeError, match="delete kaput"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


# -- save_version: schema branch + store errors ------------------------------


def test_save_version_schema_error_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("s", fixed_kwargs={"units": "v"})
            with pytest.raises(preset_ops.BadRequestError, match="object schema"):
                await preset_ops.save_version(
                    name="s",
                    fixed_kwargs=None,
                    extensions=None,
                    output_schema={"type": "string"},
                    output_schema_provided=True,
                    description=None,
                )

    asyncio.run(run())


def test_save_version_store_value_error_400(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("s", fixed_kwargs={"units": "v"})

            async def boom(self, *a, **k):
                raise ValueError("bad version payload")

            monkeypatch.setattr(type(instance.app.presets.store), "save_version", boom)
            with pytest.raises(preset_ops.BadRequestError, match="bad version payload"):
                await preset_ops.save_version(
                    name="s",
                    fixed_kwargs={"units": "z"},
                    extensions=None,
                    output_schema=None,
                    output_schema_provided=False,
                    description=None,
                )

    asyncio.run(run())


def test_save_version_store_not_found_404(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("s", fixed_kwargs={"units": "v"})

            async def boom(self, *a, **k):
                raise PresetNotFoundError("s")

            monkeypatch.setattr(type(instance.app.presets.store), "save_version", boom)
            with pytest.raises(NotFoundError, match="not found"):
                await preset_ops.save_version(
                    name="s",
                    fixed_kwargs={"units": "z"},
                    extensions=None,
                    output_schema=None,
                    output_schema_provided=False,
                    description=None,
                )

    asyncio.run(run())


# -- rollback: target-body schema branch -------------------------------------


def test_rollback_target_schema_error_400(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("r", fixed_kwargs={"units": "v"})
            await preset_ops.save_version(
                name="r",
                fixed_kwargs={"units": "w"},
                extensions=None,
                output_schema=None,
                output_schema_provided=False,
                description=None,
            )

            real_get_version = instance.app.presets.store.get_version

            async def poisoned(self, name, version):
                row = await real_get_version(name, version)
                # Present the target version with a non-object output schema so the
                # rollback's pre-commit validation rejects it (400), never re-points.
                body = PresetBody.model_validate(row.body)
                poisoned_body = body.model_copy(update={"output_schema": {"type": "string"}}).model_dump()
                return SimpleNamespace(body=poisoned_body)

            monkeypatch.setattr(type(instance.app.presets.store), "get_version", poisoned)
            with pytest.raises(preset_ops.BadRequestError, match="object schema"):
                await preset_ops.rollback_preset(name="r", version=1)

    asyncio.run(run())


def test_rollback_target_bind_error_400(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("rb", fixed_kwargs={"units": "v"})

            real_get_version = instance.app.presets.store.get_version

            async def poisoned(self, name, version):
                row = await real_get_version(name, version)
                body = PresetBody.model_validate(row.body)
                # A baked key the base tool does not accept fails the dry-run bake (400).
                poisoned_body = body.model_copy(update={"fixed_kwargs": {"not_a_param": 1}}).model_dump()
                return SimpleNamespace(body=poisoned_body)

            monkeypatch.setattr(type(instance.app.presets.store), "get_version", poisoned)
            with pytest.raises(preset_ops.BadRequestError, match="cannot bind"):
                await preset_ops.rollback_preset(name="rb", version=1)

    asyncio.run(run())


# -- rename: typed store errors + compensate branches ------------------------


def test_rename_store_exists_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def boom(self, a, b):
                raise PresetExistsError(b)

            monkeypatch.setattr(type(instance.app.presets.store), "rename_preset", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


def test_rename_store_name_conflict_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def boom(self, a, b):
                raise PresetNameConflictError(b)

            monkeypatch.setattr(type(instance.app.presets.store), "rename_preset", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


def test_rename_compensates_and_maps_exists_on_reload_failure(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def reload_boom(name):
                raise PresetExistsError(name)

            monkeypatch.setattr(instance.app.preset_manager, "reload", reload_boom)
            with pytest.raises(ConflictError, match="already exists"):
                await preset_ops.rename_preset(name="old", new_name="new")
            # The store move was compensated: the preset stays live under its old name.
            assert (await instance.app.presets.store.get_preset("old")).name == "old"

    asyncio.run(run())


def test_rename_compensates_and_maps_name_conflict_on_reload_failure(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def reload_boom(name):
                raise PresetNameConflictError(name)

            monkeypatch.setattr(instance.app.preset_manager, "reload", reload_boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


# -- delete: conflicted hard-delete failure ----------------------------------


def test_delete_conflicted_hard_delete_failure_reraises(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A stored preset whose NAME is a live tool rehydrates into quarantine.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await instance.app.versioning.store.create("preset", "weather", body.model_dump())
            await instance.app.preset_manager.rehydrate()
            assert instance.app.preset_manager.is_quarantined("weather")

            async def del_boom(self, *a, **k):
                raise RuntimeError("hard delete kaput")

            monkeypatch.setattr(type(instance.app.versioning.store), "delete", del_boom)
            with pytest.raises(RuntimeError, match="hard delete kaput"):
                await preset_ops.delete_preset(name="weather")

    asyncio.run(run())


# -- validate: create-mode verdict branches ----------------------------------


def test_validate_create_invalid_name_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            data = await preset_ops.validate_preset(name="bad/name", base_tool="weather")
            assert data["valid"] is False
            assert "invalid preset name" in data["error"]

    asyncio.run(run())


def test_validate_create_quarantined_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A quarantined name whose stored record does not resolve to an active body
            # (create-mode) yields the quarantine verdict.
            instance.app.preset_manager._quarantine["qname"] = "occupied"
            data = await preset_ops.validate_preset(name="qname", base_tool="weather")
            assert data["valid"] is False
            assert "quarantined preset" in data["error"]

    asyncio.run(run())


def test_validate_create_agent_name_collision_verdict(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            monkeypatch.setattr(preset_ops, "_agent_tool_names", lambda: {"agentic"})
            data = await preset_ops.validate_preset(name="agentic", base_tool="weather")
            assert data["valid"] is False
            assert "agent tool name" in data["error"]

    asyncio.run(run())


def test_validate_create_base_is_preset_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("basep", fixed_kwargs={"units": "v"})
            data = await preset_ops.validate_preset(name="onbasep", base_tool="basep")
            assert data["valid"] is False
            assert "is itself a preset" in data["error"]

    asyncio.run(run())


def test_validate_create_authoring_error_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # An agent base baking a field the agent does not honor — the create-mode
            # authoring gate returns an invalid verdict.
            data = await preset_ops.validate_preset(
                name="newauth", base_tool="locked_agent", fixed_kwargs={"unhonored": 1}
            )
            assert data["valid"] is False
            assert data["error"]

    asyncio.run(run())


# -- validate: version-mode authoring error ----------------------------------


def test_validate_version_authoring_error(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # An authored agent whose new ``fixed_kwargs`` bakes a field the agent does
            # not honor: the version-mode validate runs the full authoring gate.
            await _create("va", base_tool="locked_agent", fixed_kwargs={"secret_config": {"a": 1}})
            data = await preset_ops.validate_preset(name="va", fixed_kwargs={"unhonored": 1})
            assert data["valid"] is False
            assert data["error"]

    asyncio.run(run())


# -- referees: store-less 404 ------------------------------------------------


def test_referees_store_less_404(monkeypatch) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(NotFoundError, match="not found"):
                await preset_ops.preset_referees(name="nope")

    asyncio.run(run())


# -- uses / used_by cross-references -----------------------------------------


async def _seed_body(name: str, base_tool: str, fixed_kwargs: dict[str, Any]) -> None:
    # Seed an active body through the GENERIC store (like the referees oracle) so a
    # composing body can name another preset without that preset being a live tool at
    # author time.
    body = PresetBody(base_tool=base_tool, description="d", fixed_kwargs=fixed_kwargs, extensions=[])
    await instance.app.versioning.store.create("preset", name, body.model_dump())


def test_uses_used_by_reference_chain(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})
            await _seed_body("lonely", "weather", {"units": "x"})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["composer"]["uses"] == ["leaf"]
            assert rows["composer"]["used_by"] == []
            assert rows["leaf"]["uses"] == []
            assert rows["leaf"]["used_by"] == ["composer"]
            assert rows["lonely"]["uses"] == []
            assert rows["lonely"]["used_by"] == []

    asyncio.run(run())


def test_uses_excludes_self_and_non_preset_base_tools(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # ``selfref`` names itself and the base tool ``weather`` (a registered
            # non-preset tool). Self is never listed and a non-preset tool is not in
            # the population, so ``uses`` is empty.
            await _seed_body("selfref", "echo", {"tool_names": ["selfref", "weather"]})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["selfref"]["uses"] == []
            assert rows["selfref"]["used_by"] == []

    asyncio.run(run())


def test_get_row_carries_same_references_as_list_row(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})

            list_row = next(r for r in await preset_ops.list_presets() if r["name"] == "composer")
            get_row = await preset_ops.get_preset(name="composer")
            assert get_row["uses"] == list_row["uses"] == ["leaf"]
            assert get_row["used_by"] == list_row["used_by"] == []

    asyncio.run(run())


def test_used_by_matches_referees_op(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})

            leaf_row = next(r for r in await preset_ops.list_presets() if r["name"] == "leaf")
            referees = (await preset_ops.preset_referees(name="leaf"))["referees"]
            assert leaf_row["used_by"] == referees == ["composer"]

    asyncio.run(run())


def test_create_returns_uses_and_updates_used_by(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A live preset the composing create can name as a tool.
            await _create("leaf", base_tool="weather", fixed_kwargs={"units": "v"})
            created = await preset_ops.create_preset(
                name="composer",
                base_tool="authorable_agent",
                description="d",
                fixed_kwargs={"tool_names": ["leaf"]},
                extensions=[],
                output_schema=None,
            )
            assert created["uses"] == ["leaf"]
            assert created["used_by"] == []
            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["leaf"]["used_by"] == ["composer"]

    asyncio.run(run())


def test_plain_create_returns_empty_references(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            created = await preset_ops.create_preset(
                name="plain",
                base_tool="weather",
                description="d",
                fixed_kwargs={"units": "v"},
                extensions=[],
                output_schema=None,
            )
            assert created["uses"] == []
            assert created["used_by"] == []

    asyncio.run(run())


# -- declared tool references (a base tool's tool_refs extractor) -------------

# A base tool declares a ``tool_refs`` extractor at registration; the reference
# collector unions the names it reads out of a preset's ``fixed_kwargs`` with the
# builtin ``tool_names`` walk, so a DECLARED reference counts in uses/used_by,
# referees and the rename guard. The fixture ``plan_tool`` reads ``plan.refs``; a
# base tool WITHOUT a declaration keeps exactly the builtin walk.


def test_declared_refs_feed_uses_and_used_by(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("comp", "plan_tool", {"plan": {"refs": ["leaf"]}})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["comp"]["uses"] == ["leaf"]
            assert rows["comp"]["used_by"] == []
            assert rows["leaf"]["uses"] == []
            assert rows["leaf"]["used_by"] == ["comp"]

    asyncio.run(run())


def test_declared_referee_blocks_rename_and_lists_in_referees(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("leaf", base_tool="weather", fixed_kwargs={"units": "v"})
            await preset_ops.create_preset(
                name="comp",
                base_tool="plan_tool",
                description="d",
                fixed_kwargs={"plan": {"refs": ["leaf"]}},
                extensions=[],
                output_schema=None,
            )

            referees = (await preset_ops.preset_referees(name="leaf"))["referees"]
            assert referees == ["comp"]
            # The rename guard now sees the declared reference too.
            with pytest.raises(ConflictError, match="referenced by preset"):
                await preset_ops.rename_preset(name="leaf", new_name="leaf2")

    asyncio.run(run())


def test_create_response_carries_declared_refs(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("leaf", base_tool="weather", fixed_kwargs={"units": "v"})
            created = await preset_ops.create_preset(
                name="comp",
                base_tool="plan_tool",
                description="d",
                fixed_kwargs={"plan": {"refs": ["leaf"]}},
                extensions=[],
                output_schema=None,
            )
            assert created["uses"] == ["leaf"]
            assert created["used_by"] == []

    asyncio.run(run())


def test_no_declaration_ignores_nested_config(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # ``weather`` declares no extractor, so its nested config is NOT read — only
            # the builtin ``tool_names`` walk applies (empty here), unchanged behavior.
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("plainish", "weather", {"plan": {"refs": ["leaf"]}, "units": "z"})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["plainish"]["uses"] == []
            assert rows["leaf"]["used_by"] == []

    asyncio.run(run())


def test_declared_refs_extractor_raising_propagates(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("boom", "boom_tool", {})
            # The extractor raises; the op surfaces it loudly, never an empty-list fallback.
            with pytest.raises(RuntimeError, match="refs extractor kaput"):
                await preset_ops.list_presets()

    asyncio.run(run())


def test_declared_refs_non_string_entry_raises(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("comp", "plan_tool", {"plan": {"refs": [123]}})
            # A declaration returning garbage is a plugin bug, raised loudly.
            with pytest.raises(TypeError, match="non-string reference"):
                await preset_ops.list_presets()

    asyncio.run(run())


# -- version tags: bad version + store-less 501 ------------------------------


def test_set_version_tags_non_int_version_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(preset_ops.BadRequestError, match="version must be an integer"):
                await preset_ops.set_preset_version_tags(name="x", version="abc", tags=[])

    asyncio.run(run())


def test_set_version_tags_store_less_501(monkeypatch) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(
                preset_ops.NotSupportedError, match="versioned-document store is not configured"
            ) as exc_info:
                await preset_ops.set_preset_version_tags(name="x", version="1", tags=[])
            assert exc_info.value.extra["code"] == "versioning-not-configured"

    asyncio.run(run())


# -- per-base-tool write validator -------------------------------------------

# A base-tool plugin registers a write validator for its base tool; the preset
# write path consults it on the full body before persisting. The registry is reset
# each ``start()``, so each ``app_context`` opens with a clean one and a test
# registers a validator for the fixture ``weather`` base tool.


def test_create_rejected_by_write_validator_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return ["weather rejects this", "second reason"]

            instance.app.presets.register_write_validator("weather", validator)
            with pytest.raises(preset_ops.BadRequestError) as exc_info:
                await _create("w", fixed_kwargs={"units": "v"})
            # Issues verbatim, one per line, and no row persisted.
            assert exc_info.value.message == "weather rejects this\nsecond reason"
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("w")

    asyncio.run(run())


def test_create_accepted_when_write_validator_passes(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return []

            instance.app.presets.register_write_validator("weather", validator)
            await _create("w", fixed_kwargs={"units": "v"})
            assert (await instance.app.presets.store.get_preset("w")).name == "w"

    asyncio.run(run())


def test_create_untouched_when_no_write_validator(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # No validator registered for the base tool — the write path runs ungated.
            await _create("w", fixed_kwargs={"units": "v"})
            assert (await instance.app.presets.store.get_preset("w")).name == "w"

    asyncio.run(run())


def test_create_write_validator_exception_propagates(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                raise RuntimeError("validator kaput")

            instance.app.presets.register_write_validator("weather", validator)
            # A validator exception surfaces loudly — never swallowed, never a pass.
            with pytest.raises(RuntimeError, match="validator kaput"):
                await _create("w", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_save_version_rejected_by_write_validator_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            issues: list[str] = []

            async def validator(body):
                return list(issues)

            instance.app.presets.register_write_validator("weather", validator)
            await _create("w", fixed_kwargs={"units": "v"})
            issues.append("save rejected")
            with pytest.raises(preset_ops.BadRequestError) as exc_info:
                await preset_ops.save_version(
                    name="w",
                    fixed_kwargs={"units": "z"},
                    extensions=None,
                    output_schema=None,
                    output_schema_provided=False,
                    description=None,
                )
            assert exc_info.value.message == "save rejected"

    asyncio.run(run())


def test_rollback_rejected_by_write_validator_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            issues: list[str] = []

            async def validator(body):
                return list(issues)

            instance.app.presets.register_write_validator("weather", validator)
            await _create("w", fixed_kwargs={"units": "v"})
            await preset_ops.save_version(
                name="w",
                fixed_kwargs={"units": "z"},
                extensions=None,
                output_schema=None,
                output_schema_provided=False,
                description=None,
            )
            issues.append("rollback rejected")
            with pytest.raises(preset_ops.BadRequestError) as exc_info:
                await preset_ops.rollback_preset(name="w", version=1)
            assert exc_info.value.message == "rollback rejected"

    asyncio.run(run())


def test_validate_verdict_carries_write_validator_issues(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return ["draft would be rejected"]

            instance.app.presets.register_write_validator("weather", validator)
            data = await preset_ops.validate_preset(name="w", base_tool="weather", fixed_kwargs={"units": "v"})
            assert data["valid"] is False
            assert data["error"] == "draft would be rejected"

    asyncio.run(run())
