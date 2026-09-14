"""Preset authoring over ``sandbox_exec`` (a future-annotations, ``ExecResult``-typed
base): a plain preset authors and inherits the base output schema, and an
``input_schema`` preset authors, validates the caller object, and routes it into
the base's payload arg."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from tai42_contract.sandbox import SandboxUnavailableError
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ._manager_fixtures import FakeVersioningPg

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
            # must SUCCEED through the branch-bind output-schema derivation, which resolves
            # ``ExecResult`` in scope.
            await mgr.register("sbx_plain", "sandbox_exec", _SANDBOX_FIXED_KWARGS, [], "run a fixed command")
            assert "sbx_plain" in await app.tools.get_tools()
            # The preset inherits the base tool's ``ExecResult`` output schema.
            tool = await app.tools.get_tool("sbx_plain")
            assert set((tool.output_schema or {}).get("properties", {})) == {"exit_code", "stdout", "stderr"}

    asyncio.run(run())


def test_input_schema_preset_over_sandbox_exec_authors_validates_and_routes(
    pg: FakeVersioningPg, monkeypatch: pytest.MonkeyPatch
):
    # ``sandbox_exec`` is a ``fenced`` tool, so the run-seam tier fence admits only an
    # admin. This test exercises the preset ROUTING mechanics, not the fence: run with
    # access control OFF (every sandbox deployment's posture), so the caller resolves as an
    # admin and the fence is a no-op — the schema-validation and payload-routing behaviour
    # is what is asserted. The fence itself is covered in tests/tools/test_run_tier_fence.py.
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    reset_all_settings()

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

    try:
        asyncio.run(run())
    finally:
        # Reading settings under ``ACCESS_CONTROL_ENABLE=false`` above cached the
        # disabled ``AccessControlSettings`` in the process-wide settings cache;
        # ``monkeypatch`` restores the env but not the cache, so drop it here or the
        # disabled gate leaks into every later test.
        reset_all_settings()
