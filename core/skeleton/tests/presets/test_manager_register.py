"""``PresetManager.register`` binds a runnable tool, rejects a baked key, serves
secret and non-secret preset dispatch, validates output schemas, and exposes a
versioned preset's typed schema."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ._manager_fixtures import FakeVersioningPg, _create_versioned, _manifest


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
