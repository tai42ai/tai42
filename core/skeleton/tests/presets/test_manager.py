"""The preset register/reload engine (:class:`PresetManager`).

Every case drives the REAL bind path — ``app.app_context`` with a live
``ToolRegistry``, the true ``PostgresVersionedStore`` + ``PresetStoreView`` over
the stateful fake Postgres (the ``pg`` fixture) — so the engine is exercised
end-to-end: a preset becomes a runnable MCP tool, its baked kwargs are served and
a baked key rejected, extension combos branch off the bare name, versioning
carries the whole body forward, reload/rollback re-serve the right kwargs, a
failed re-register never drops the live tool, and a stale preset is quarantined
rather than bricking boot.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.tools.base import Tool
from tai42_contract.agent.base import PresetSpec
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetNameConflictError, PresetNotFoundError
from tai42_contract.sandbox import SandboxUnavailableError

from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding import UnknownToolError

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


def _live_tool_names() -> list[str]:
    """Every tool name held by the live FastMCP provider, WITH duplicates — so a
    reload that leaked a second copy of a branch shows up as a repeated name (a
    plain ``get_tools()`` dict would collapse it)."""
    components = app.fastmcp.local_provider._components
    return [c.name for c in components.values() if isinstance(c, Tool)]


async def _create_versioned(name: str, base_tool: str, fixed_kwargs, extensions, description="d") -> None:
    """Persist a versioned preset AND register it — the create route's two steps."""
    await app.presets.store.create_preset(
        PresetSpec(name=name, description=description, base_tool=base_tool, fixed_kwargs=fixed_kwargs),
        extensions=extensions,
    )
    body = await app.presets.store.get_active_body(name)
    await app.preset_manager.register(name, body.base_tool, body.fixed_kwargs, body.extensions, body.description)


# -- runnable + baked kwargs -------------------------------------------------


def test_register_binds_runnable_tool_and_rejects_baked_key(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await mgr.register("paris", "weather", {"units": "imperial"}, [], "Paris weather")

            assert "paris" in await app.tools.get_tools()
            # The baked value is served as a fixed constant...
            assert await app.tools.run_tool("paris", {"city": "paris"}) == {"city": "paris", "units": "imperial"}
            # ...and a caller that passes the baked key is REJECTED, never overriding it.
            with pytest.raises(TypeError):
                await app.tools.run_tool("paris", {"city": "paris", "units": "metric"})
            # ``register`` binds the live tool only — it never writes a preset row
            # (boot seeds the default role documents, a different kind).
            assert [d for d in pg.documents if d["kind"] == "preset"] == []

    asyncio.run(run())


_SECRET_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo", "vault"]}],
}


def _secret_manifest() -> Manifest:
    return Manifest.model_validate(_SECRET_MANIFEST)


def test_secret_preset_dispatch_returns_wrapper_intact_to_the_seam(pg: FakeVersioningPg):
    # A PRESET over a secret-returning tool run by name through the in-process seam
    # returns the value with its SecretValue wrapper INTACT — the forwarding fn's
    # re-entry of the parent tool's convert_result does not reveal in-process. This
    # is the seam every recorder/adapter masks (and the sync door reveals): its mask
    # is a real transform here, not a no-op over already-revealed plaintext.
    from tai42_contract.secrets import SecretValue, contains_secrets

    async def run():
        async with app.app_context(_secret_manifest()):
            await app.preset_manager.register("acme_vault", "vault", {"account": "acme"}, [], "Acme vault")
            result = await app.tools.run_tool("acme_vault", {})
            assert result["account"] == "acme"
            assert isinstance(result["token"], SecretValue)
            assert contains_secrets(result) is True

    asyncio.run(run())


def test_non_secret_preset_has_zero_drift(pg: FakeVersioningPg):
    # A preset over a NON-secret tool keeps the exact ``_tool_result_value`` path: the
    # armed gate stows nothing, and both a dict and a scalar preset return their plain
    # values byte-for-byte, unchanged by the secret gate.
    async def run():
        async with app.app_context(_secret_manifest()):
            mgr = app.preset_manager
            await mgr.register("paris", "weather", {"units": "imperial"}, [], "Paris weather")
            await mgr.register("shout", "echo", {}, [], "Echo")

            assert await app.tools.run_tool("paris", {"city": "x"}) == {"city": "x", "units": "imperial"}
            # A scalar preset return still unwraps through the wrap_result path to its
            # bare value — no drift from the gate.
            assert await app.tools.run_tool("shout", {"text": "hi"}) == "hi"

    asyncio.run(run())


def test_secret_preset_output_schema_validates_revealed_value(pg: FakeVersioningPg):
    # A preset over a secret tool whose output_schema constrains the secret field to a
    # pattern the REAL value satisfies (``^tok-``) but the ``[secret]`` placeholder does
    # not: the guard validates the REVEALED value, so dispatch succeeds and the wrapper
    # rides out intact. Validating the masked projection would false-reject the placeholder.
    from tai42_contract.secrets import SecretValue, contains_secrets

    schema = {
        "type": "object",
        "properties": {"account": {"type": "string"}, "token": {"type": "string", "pattern": "^tok-"}},
        "required": ["account", "token"],
    }

    async def run():
        async with app.app_context(_secret_manifest()):
            await app.preset_manager.register(
                "acme_vault", "vault", {"account": "acme"}, [], "Acme vault", output_schema=schema
            )
            result = await app.tools.run_tool("acme_vault", {})
            assert result["account"] == "acme"
            assert isinstance(result["token"], SecretValue)
            assert contains_secrets(result) is True

    asyncio.run(run())


def test_secret_preset_output_schema_rejects_when_real_value_violates(pg: FakeVersioningPg):
    # The output_schema demands ``minLength: 6`` on the secret field. The REAL token
    # ``tok-x`` (5 chars) VIOLATES it while the ``[secret]`` placeholder (8 chars)
    # SATISFIES it — a masked-projection guard would be silently defeated. The guard
    # judges the REAL value, so dispatch raises loudly — but the raised error redacts
    # the offending instance: the real token never rides the message, repr, or chain.
    from tai42_contract.secrets import SECRET_PLACEHOLDER
    from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

    schema = {
        "type": "object",
        "properties": {"account": {"type": "string"}, "token": {"type": "string", "minLength": 6}},
        "required": ["account", "token"],
    }

    async def run():
        async with app.app_context(_secret_manifest()):
            await app.preset_manager.register(
                "x_vault", "vault", {"account": "x"}, [], "Short vault", output_schema=schema
            )
            with pytest.raises(JsonSchemaValidationError) as caught:
                await app.tools.run_tool("x_vault", {})

            exc = caught.value
            # The real token is absent from every rendering of the error...
            assert "tok-x" not in str(exc)
            assert "tok-x" not in repr(exc)
            # ...the caught secret-bearing error survives on no chain attribute...
            assert exc.__cause__ is None
            assert exc.__context__ is None
            # ...the json path is kept, and the offending value is the placeholder, not the raw.
            assert exc.json_path == "$.token"
            assert exc.offending_value == SECRET_PLACEHOLDER

    asyncio.run(run())


def test_non_secret_preset_output_schema_validates_structured_content(pg: FakeVersioningPg):
    # A NON-secret preset's dispatch arms the gate but stows nothing, so the guard
    # validates the structured content exactly as before: a conforming result passes
    # and a violating one raises loudly.
    from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

    conforming = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string"}},
        "required": ["city", "units"],
    }
    violating = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string", "maxLength": 2}},
        "required": ["city", "units"],
    }

    async def run():
        async with app.app_context(_secret_manifest()):
            mgr = app.preset_manager
            await mgr.register("p_ok", "weather", {"units": "imperial"}, [], "ok", output_schema=conforming)
            assert await app.tools.run_tool("p_ok", {"city": "x"}) == {"city": "x", "units": "imperial"}

            await mgr.register("p_bad", "weather", {"units": "imperial"}, [], "bad", output_schema=violating)
            with pytest.raises(JsonSchemaValidationError) as caught:
                await app.tools.run_tool("p_bad", {"city": "x"})
            # The non-secret branch still quotes the offending instance verbatim — the
            # secret-branch redaction did NOT widen to this path.
            assert "imperial" in str(caught.value)

    asyncio.run(run())


def test_versioned_preset_runnable_and_typed_schema(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            await _create_versioned("wv", "weather", {"units": "imperial"}, [])
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "imperial"}

            # The baked key is HIDDEN from the exposed schema; the remaining arg
            # keeps its real typed schema (name + type), not one opaque blob.
            tool = await app.tools.get_tool("wv")
            props = tool.parameters.get("properties", {})
            assert "units" not in props
            assert props["city"]["type"] == "string"

    asyncio.run(run())


# -- spec map is authoritative + in lockstep ---------------------------------


def test_spec_map_serves_active_baked_kwargs(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await mgr.register("eph", "weather", {"units": "eph"}, [], "d")
            await _create_versioned("ver", "weather", {"units": "v1"}, [], description="Version one")

            # The spec map is the source of truth for baked kwargs.
            assert mgr.baked_kwargs("eph") == {"units": "eph"}
            assert mgr.baked_kwargs("ver") == {"units": "v1"}
            assert mgr.get_spec("ver").description == "Version one"
            assert set(mgr.registered_names()) == {"eph", "ver"}

            # The map stays in lockstep with the active version after an edit.
            await app.presets.store.save_version("ver", fixed_kwargs={"units": "v2"})
            await mgr.reload("ver")
            assert mgr.baked_kwargs("ver") == {"units": "v2"}

            with pytest.raises(PresetNotFoundError):
                mgr.get_spec("nope")

    asyncio.run(run())


# -- reload / rollback serve the right kwargs --------------------------------


def test_reload_and_rollback_serve_right_kwargs(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])

            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v2"}

            await store.rollback("wv", 1)
            await mgr.reload("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v1"}

    asyncio.run(run())


def test_active_version_retained_across_create_save_rollback_reload(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            # A fresh create mints version 1 and the engine retains it beside the body.
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            assert mgr.active_version("wv") == 1
            # A plain (non-preset) tool has no retained version.
            assert mgr.active_version("weather") is None

            # A save + reload advances the retained version to the new active one.
            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")
            assert mgr.active_version("wv") == 2

            # A rollback + reload re-points the retained version at the rolled-back one
            # (the active pointer, not MAX).
            await store.rollback("wv", 1)
            await mgr.reload("wv")
            assert mgr.active_version("wv") == 1

            # Teardown drops the retained version in lockstep with the spec.
            await mgr.remove("wv")
            assert mgr.active_version("wv") is None

    asyncio.run(run())


def test_active_version_survives_rehydrate(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")
            assert mgr.active_version("wv") == 2

            # A reload_config wipe + rehydrate rebuilds the version from the store's
            # single version-aware batched read — the retained value is not lost.
            await mgr.remove("wv")
            await mgr.rehydrate()
            assert mgr.active_version("wv") == 2

    asyncio.run(run())


def test_run_tool_stamps_registered_preset_with_active_version(pg: FakeVersioningPg, monkeypatch):
    """The run chokepoint attributes a registered preset dispatch with its version;
    a plain tool is left unstamped."""

    class _SpyWriter:
        def __init__(self) -> None:
            self.stamps: list[dict] = []

        def current_trace_id(self):
            return "t"

        def trace_attributes(self, *, name, tags, metadata, user_id=None, session_id=None):
            self.stamps.append({"tags": tags, "metadata": metadata})
            from contextlib import nullcontext

            return nullcontext()

    class _SpyMonitoring:
        def __init__(self, writer) -> None:
            self.writer = writer

    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")

            writer = _SpyWriter()
            import tai42_skeleton.monitoring as monitoring_mod

            monkeypatch.setattr(monitoring_mod, "get_monitoring", lambda: _SpyMonitoring(writer))

            # A registered-preset dispatch stamps preset:/preset-v: with the active version.
            await app.tools.run_tool("wv", {"city": "x"})
            preset_stamps = [s for s in writer.stamps if any(t.startswith("preset:") for t in (s["tags"] or []))]
            assert len(preset_stamps) == 1
            assert preset_stamps[0]["tags"] == ["preset:wv", "preset-v:2"]
            assert preset_stamps[0]["metadata"]["preset_version"] == "2"

            # A plain (non-preset) tool dispatch stamps NO preset attribution.
            writer.stamps.clear()
            await app.tools.run_tool("weather", {"city": "x", "units": "z"})
            assert not [s for s in writer.stamps if any(t.startswith("preset:") for t in (s["tags"] or []))]

    asyncio.run(run())


def test_save_version_numbering_is_max_plus_one_post_rollback(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            for units in ("v2", "v3", "v4", "v5"):
                await store.save_version("wv", fixed_kwargs={"units": units})
            await store.rollback("wv", 2)  # active trails MAX
            new = await store.save_version("wv", fixed_kwargs={"units": "v6"})
            assert new.version == 6  # MAX+1, not active(2)+1
            await mgr.reload("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v6"}

    asyncio.run(run())


# -- a wrapper branch of a preset keeps the preset's description -------------


def test_wrapper_branch_of_preset_keeps_preset_description(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # ``exta`` is a ``functools.wraps`` wrapper, so its branch inherits the
            # base callable's docstring. The adoption guard must compare against the
            # BRANCH BASE callable's docstring (not the ``Tool`` object's class
            # docstring), so the wrapper is recognized as authoring no new
            # description and the preset's own description survives onto the branch.
            await _create_versioned("shouty", "echo", {}, [["exta"]], description="Preset desc")

            base = await app.tools.get_tool("shouty")
            branch = await app.tools.get_tool("shouty_exta")
            assert base.description == "Preset desc"
            assert branch.description == "Preset desc"

    asyncio.run(run())


# -- extensions survive versioning (two independent combos) ------------------


def test_extensions_two_combos_survive_versioning(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("shouty", "echo", {}, [["exta"], ["extb"]])

            # Bare name runnable + BOTH independent branches + NO stacked branch.
            tools = set(await app.tools.get_tools())
            assert {"shouty", "shouty_exta", "shouty_extb"} <= tools
            assert "shouty_exta_extb" not in tools
            assert await app.tools.run_tool("shouty", {"text": "hi"}) == "hi"
            assert await app.tools.run_tool("shouty_exta", {"text": "hi"}) == "hi|a"
            assert await app.tools.run_tool("shouty_extb", {"text": "hi"}) == "hi|b"

            # Save a new version WITHOUT passing extensions, then reload: the new
            # active body carried base_tool + BOTH combos forward.
            await store.save_version("shouty", fixed_kwargs={})
            await mgr.reload("shouty")
            tools = set(await app.tools.get_tools())
            assert {"shouty", "shouty_exta", "shouty_extb"} <= tools
            assert "shouty_exta_extb" not in tools
            assert await app.tools.run_tool("shouty_exta", {"text": "yo"}) == "yo|a"

            # Each branch is bound EXACTLY once (the reload's teardown ran before
            # re-register — no leaked pre-reload duplicate), and the base's
            # _extend_tools holds exactly one entry per branch.
            names = _live_tool_names()
            assert names.count("shouty_exta") == 1
            assert names.count("shouty_extb") == 1
            branches = {b for b, base in app._tool_registry._extend_tools.items() if base == "shouty" and b != "shouty"}
            assert branches == {"shouty_exta", "shouty_extb"}

    asyncio.run(run())


# -- remove tears down base + branches ---------------------------------------


def test_remove_tears_down_base_and_branches(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("shouty", "echo", {}, [["exta"], ["extb"]])
            assert {"shouty", "shouty_exta", "shouty_extb"} <= set(await app.tools.get_tools())

            await mgr.remove("shouty")

            tools = set(await app.tools.get_tools())
            assert not ({"shouty", "shouty_exta", "shouty_extb"} & tools)
            assert not mgr.is_registered("shouty")
            for name in ("shouty", "shouty_exta", "shouty_extb"):
                with pytest.raises(UnknownToolError):
                    await app.tools.run_tool(name, {"text": "hi"})

    asyncio.run(run())


# -- body description -> tool description (re-projected on reload) ------------


def test_body_description_projects_to_tool_and_survives_reload(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [], description="Weather v1")
            assert (await app.tools.get_tool("wv")).description == "Weather v1"

            # A simulated reload wipes the runtime tool; rehydrate re-projects the
            # description from the persisted body.
            await mgr.remove("wv")
            await mgr.rehydrate()
            assert (await app.tools.get_tool("wv")).description == "Weather v1"

    asyncio.run(run())


# -- name-collision guard raises before any store write ----------------------


def test_name_conflict_raises_before_store_write(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # "weather" is a live non-preset base tool.
            assert await app.preset_manager.name_conflicts("weather") is True
            with pytest.raises(PresetNameConflictError):
                await app.presets.store.create_preset(
                    PresetSpec(name="weather", description="d", base_tool="echo", fixed_kwargs={}),
                    extensions=[],
                )
            # No preset row persisted (boot-seeded role documents are a different kind).
            assert [d for d in pg.documents if d["kind"] == "preset"] == []

    asyncio.run(run())


def test_register_rejects_invalid_name(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # A preset name is a live tool name + a route segment, so the manager
            # rejects a name outside the tool-name-safe alphabet/length before any
            # bind (the create route's 400 guard shares this rule).
            for bad in ("a/b", "x" * 65, "bad name"):
                with pytest.raises(ValueError, match="invalid preset name"):
                    await app.preset_manager.register(bad, "echo", {}, [], "d")

    asyncio.run(run())


def test_register_failure_leaves_no_partial_registration(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # An unknown extension makes the branch bind raise inside register.
            with pytest.raises(TaiValidationError):
                await mgr.register("bad", "echo", {}, [["ghost_ext"]], "d")
            tools = set(await app.tools.get_tools())
            assert "bad" not in tools
            assert not mgr.is_registered("bad")
            # The structured-registry seed was rolled back too.
            assert list(app._tool_registry.tool_extensions_iterator("bad")) == []

    asyncio.run(run())


# -- edit-path re-register is atomic (never drops the live tool) -------------


def test_edit_path_reload_failure_restores_old_registration(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "echo", {}, [["exta"]])
            assert await app.tools.run_tool("wv_exta", {"text": "hi"}) == "hi|a"

            # A new version whose extensions reference an unknown ext will fail the
            # reload's re-register; the committed store bump is NOT unwound.
            await store.save_version("wv", extensions=[["ghost_ext"]])
            with pytest.raises(TaiValidationError):
                await mgr.reload("wv")

            # The PRIOR registration survived: base AND its branch still runnable,
            # and the spec map still holds the old body (the restore ran).
            assert await app.tools.run_tool("wv", {"text": "hi"}) == "hi"
            assert await app.tools.run_tool("wv_exta", {"text": "hi"}) == "hi|a"
            assert mgr.get_spec("wv").extensions == [["exta"]]

    asyncio.run(run())


# -- rehydrate durability (store-backed presets survive) ---------------------


def test_rehydrate_reregisters_only_store_backed_presets(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # A registration with no store row (bound directly, never persisted)
            # alongside a persisted one.
            await mgr.register("unbacked", "weather", {"units": "e"}, [], "d")
            await _create_versioned("ver", "weather", {"units": "v"}, [])

            # Simulate reload_config wiping every runtime preset, then rehydrate.
            for name in ("unbacked", "ver"):
                await mgr.remove(name)
            await mgr.rehydrate()

            tools = set(await app.tools.get_tools())
            assert "ver" in tools
            assert "unbacked" not in tools
            assert set(mgr.registered_names()) == {"ver"}  # only the store-backed one rebuilt
            assert await app.tools.run_tool("ver", {"city": "x"}) == {"city": "x", "units": "v"}

    asyncio.run(run())


def test_rehydrate_skips_record_whose_active_body_is_absent(pg: FakeVersioningPg, monkeypatch, caplog):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # Two persisted presets. ``list_presets`` and ``list_active_versioned_bodies``
            # are two separate store reads, so a delete landing between them leaves a
            # record whose active body is already gone. Model that read-skew: both
            # records survive in ``list_presets`` while ``absent`` is dropped from
            # the active version+body map.
            await _create_versioned("present", "weather", {"units": "v"}, [])
            await _create_versioned("absent", "weather", {"units": "g"}, [])

            # Simulate reload_config wiping every runtime preset before rehydrate.
            for name in ("present", "absent"):
                await mgr.remove(name)

            real_bodies = app.presets.list_active_versioned_bodies

            async def _bodies_without_absent():
                bodies = await real_bodies()
                bodies.pop("absent", None)
                return bodies

            monkeypatch.setattr(app.presets, "list_active_versioned_bodies", _bodies_without_absent)

            # A record with no active body must be SKIPPED, never a bare
            # ``bodies[rec.name]`` KeyError that aborts the whole boot/reload.
            with caplog.at_level(logging.WARNING, logger="tai42_skeleton.presets.manager"):
                await mgr.rehydrate()

            # The skipped record is neither registered nor quarantined — just dropped.
            assert not mgr.is_registered("absent")
            assert not mgr.is_quarantined("absent")
            assert "absent" not in await app.tools.get_tools()
            # The present-body preset rebuilt normally and stays runnable.
            assert mgr.is_registered("present")
            assert await app.tools.run_tool("present", {"city": "x"}) == {"city": "x", "units": "v"}
            # The skip was logged loudly for the missing name.
            assert "absent" in caplog.text

    asyncio.run(run())


# -- quarantine: the three stale-preset causes -------------------------------


def test_rehydrate_quarantines_foreign_name(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # Seed a persisted preset whose NAME is a live base tool. The store's
            # create-time collision guard (rightly) blocks this via the preset
            # view, so seed through the generic store to model a name that only
            # BECAME a base tool after the preset was persisted.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await app.versioning.store.create("preset", "weather", body.model_dump())
            await mgr.rehydrate()  # app still boots — no raise

            assert mgr.is_quarantined("weather")
            # Not registered as a preset; the foreign base tool still owns the name.
            assert not mgr.is_registered("weather")
            assert await app.tools.run_tool("weather", {"city": "x"}) == {"city": "x", "units": "metric"}

            # The DELETE-conflicted branch drops the quarantine entry immediately.
            mgr.drop_quarantine("weather")
            assert not mgr.is_quarantined("weather")

    asyncio.run(run())


def test_rehydrate_quarantines_missing_base_tool(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await app.presets.store.create_preset(
                PresetSpec(name="orphan", description="d", base_tool="gone_tool", fixed_kwargs={}), extensions=[]
            )
            await mgr.rehydrate()
            assert mgr.is_quarantined("orphan")
            assert not mgr.is_registered("orphan")
            assert "orphan" not in await app.tools.get_tools()

    asyncio.run(run())


def test_rehydrate_quarantines_preset_owned_base(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # A valid versioned preset, plus another whose base_tool is that preset
            # — a preset may not be another preset's base, in EITHER load order.
            await _create_versioned("base_preset", "weather", {"units": "v"}, [])
            await app.presets.store.create_preset(
                PresetSpec(name="chained", description="d", base_tool="base_preset", fixed_kwargs={}), extensions=[]
            )
            await mgr.remove("base_preset")  # clear runtime state before the rehydrate
            await mgr.rehydrate()

            assert mgr.is_registered("base_preset")  # the legitimate one rebuilt
            assert mgr.is_quarantined("chained")  # the preset-on-preset rejected
            assert not mgr.is_registered("chained")

    asyncio.run(run())


def test_rehydrate_idempotent_self_registration_no_conflict(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v"}, [["exta"]])
            # A second rehydrate (as a redundant reload would trigger) rebuilds the
            # same preset cleanly — no conflict, still runnable, exactly one branch.
            await mgr.rehydrate()
            assert mgr.is_registered("wv")
            assert not mgr.is_quarantined("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v"}
            assert _live_tool_names().count("wv_exta") == 1

    asyncio.run(run())


# -- reconcile after a scoped MCP change -------------------------------------


# -- quarantine reason storage -----------------------------------------------


def test_quarantine_reason_readable_and_cleared(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # A stored preset whose name is a live base tool quarantines on rehydrate.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await app.versioning.store.create("preset", "weather", body.model_dump())
            await mgr.rehydrate()

            assert mgr.is_quarantined("weather")
            reason = mgr.quarantine_reason("weather")
            assert reason is not None
            assert "occupied by an existing tool" in reason
            # A non-quarantined / unknown name carries no reason.
            assert mgr.quarantine_reason("nope") is None
            # Drop clears BOTH membership and the reason.
            mgr.drop_quarantine("weather")
            assert not mgr.is_quarantined("weather")
            assert mgr.quarantine_reason("weather") is None

    asyncio.run(run())


def test_quarantine_reason_bulk_reset_is_coherent(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await app.presets.store.create_preset(
                PresetSpec(name="orphan", description="d", base_tool="gone_tool", fixed_kwargs={}), extensions=[]
            )
            await mgr.rehydrate()
            assert "gone_tool" in (mgr.quarantine_reason("orphan") or "")

            # A second rehydrate wipes the map wholesale then rebuilds it — still
            # quarantined, same reason, no stale entry accreted.
            await mgr.rehydrate()
            assert mgr.is_quarantined("orphan")
            assert "gone_tool" in (mgr.quarantine_reason("orphan") or "")
            assert set(mgr.quarantined_names()) == {"orphan"}

    asyncio.run(run())


def test_reconcile_quarantines_on_reregister_failure(pg: FakeVersioningPg, monkeypatch):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v"}, [])

            # The base tool is still live, but re-registration fails (the environment
            # changed after the reload) — the preset is quarantined, never left
            # half-bound to a stale closure.
            async def _boom(*args, **kwargs):
                raise TaiValidationError("reconcile re-register failure")

            monkeypatch.setattr(mgr, "_register", _boom)
            await mgr.reconcile_bases({"weather"})

            assert mgr.is_quarantined("wv")
            assert not mgr.is_registered("wv")

    asyncio.run(run())


# -- authoring over ``sandbox_exec`` (a future-annotations, ExecResult-typed base) -----

# ``sandbox_exec`` is declared under ``from __future__ import annotations`` and returns the
# custom ``ExecResult`` model, so its function annotations are STRING forward-refs. Authoring
# ANY preset over it drives the full register/branch-bind path (``_baked_partial`` →
# ``_derive_output_schema``), which must resolve those forward-refs against the base function's
# own module globals — never leave ``"ExecResult"`` an unresolvable string that pydantic's
# schema parse would raise ``NameError`` on. The base binds with no provider (no session is
# created here); only a RUN would need one.
_SANDBOX_MANIFEST = {
    "tools": [{"title": "sbx", "module": "tai42_skeleton.tools.builtin.sandbox_exec", "include": ["sandbox_exec"]}],
}


def _sandbox_manifest() -> Manifest:
    return Manifest.model_validate(_SANDBOX_MANIFEST)


# A caller input contract for an ``input_schema`` preset: one required string, no extras.
_SANDBOX_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"msg": {"type": "string"}},
    "required": ["msg"],
    "additionalProperties": False,
}
_SANDBOX_FIXED_KWARGS = {"argv": ["echo"], "image": "img@sha256:" + "0" * 64}


def test_plain_preset_over_sandbox_exec_authors(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_sandbox_manifest()):
            mgr = app.preset_manager
            # A plain preset bakes the required ``argv``/``image`` as fixed constants. Authoring
            # must SUCCEED through the branch-bind output-schema derivation (previously a
            # ``NameError: name 'ExecResult' is not defined``).
            await mgr.register("sbx_plain", "sandbox_exec", _SANDBOX_FIXED_KWARGS, [], "run a fixed command")
            assert "sbx_plain" in await app.tools.get_tools()
            # The preset inherits the base tool's ``ExecResult`` output schema.
            tool = await app.tools.get_tool("sbx_plain")
            assert set((tool.output_schema or {}).get("properties", {})) == {"exit_code", "stdout", "stderr"}

    asyncio.run(run())


def test_input_schema_preset_over_sandbox_exec_authors_validates_and_routes(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_sandbox_manifest()):
            mgr = app.preset_manager
            # An input_schema preset: the authored schema becomes the exposed tool's OWN input
            # contract, its validated object routed into the base's ``input`` payload arg.
            await mgr.register(
                "sbx_typed",
                "sandbox_exec",
                _SANDBOX_FIXED_KWARGS,
                [],
                "echo the validated payload",
                input_schema=_SANDBOX_INPUT_SCHEMA,
            )
            assert "sbx_typed" in await app.tools.get_tools()
            tool = await app.tools.get_tool("sbx_typed")
            # The exposed tool advertises the AUTHORED schema as its input contract and inherits
            # the base tool's ``ExecResult`` output schema (so the result reconstructs typed).
            assert tool.parameters == _SANDBOX_INPUT_SCHEMA
            assert set((tool.output_schema or {}).get("properties", {})) == {"exit_code", "stdout", "stderr"}

            # A caller object violating the schema is rejected LOUDLY before routing — never
            # forwarded into the base tool (wrong type, then a forbidden extra field).
            with pytest.raises(FastMCPValidationError):
                await app.tools.run_tool("sbx_typed", {"msg": 123})
            with pytest.raises(FastMCPValidationError):
                await app.tools.run_tool("sbx_typed", {"msg": "x", "unexpected": 1})

            # A VALID caller passes validation and ROUTES into the base tool, which — with no
            # sandbox provider registered here — fails loud at the acquisition chokepoint,
            # proving the payload reached the base rather than being rejected at validation.
            with pytest.raises(SandboxUnavailableError):
                await app.tools.run_tool("sbx_typed", {"msg": "routed"})

    asyncio.run(run())
