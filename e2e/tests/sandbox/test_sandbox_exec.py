"""The ``sandbox_exec`` base tool end to end: it runs IN the sandbox, it is loud on a
provider-less door, its per-tool ``input_schema`` preset delivery validates the caller,
and its authoring/invocation fence is a no-op only where the platform does not fence.

``sandbox_exec`` acquires a session through the same kit chokepoint every consumer does,
runs ``argv`` in it, and returns the :class:`ExecResult`. A DELIVERY names concrete tools
as PRESETS over it (an authored ``input_schema`` becomes the exposed tool's input contract,
routed into the base tool's ``payload_arg``). Backend-invariant, so ``backendless``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.manifests import build_sandbox_stack
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

# A digest-pinned image reference — inert under the fake provider (the host is the execution
# environment) but the model requires a digest, never a bare tag.
_IMAGE = "img@sha256:" + "0" * 64

# A caller input contract for an ``input_schema`` preset: one required string, no extras.
_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"msg": {"type": "string"}},
    "required": ["msg"],
    "additionalProperties": False,
}


def _sandbox_exec_no_provider(res, variants):
    """``build_sandbox_stack`` with the provider slot REMOVED — ``sandbox_exec`` binds but
    ``require_sandbox`` finds no provider, so the base tool fails loud."""
    config = build_sandbox_stack(res, variants)
    del config.manifest["sandbox_module"]
    return config


# ---- it runs in the sandbox ----------------------------------------------


async def test_sandbox_exec_runs_argv_in_a_session(sandbox_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    token = uniq("exec")
    async with sandbox_stack.mcp() as mcp:
        result = await mcp.call_tool("sandbox_exec", {"argv": ["python", "-c", f"print({token!r})"], "image": _IMAGE})
    assert result.data.exit_code == 0, result.data
    assert token in result.data.stdout, result.data


async def test_sandbox_exec_is_loud_without_a_provider(fresh_stack) -> None:
    stack: TaiStack = fresh_stack(_sandbox_exec_no_provider)
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "sandbox_exec", {"argv": ["python", "-c", "pass"], "image": _IMAGE}, raise_on_error=False
        )
    assert result.is_error, result
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert "no sandbox provider is registered" in text, text


# ---- typed per-tool input schema -----------------------------------------


async def test_input_schema_preset_validates_the_caller_and_routes_payload(
    sandbox_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = sandbox_stack.api()
    name = uniq("echo_stdin")
    # Author a preset over sandbox_exec whose input_schema is the exposed tool's contract; the
    # baked argv echoes the JSON payload (delivered on stdin) back out.
    created = await api.request_raw(
        "POST",
        "/api/presets",
        json={
            "name": name,
            "base_tool": "sandbox_exec",
            "description": "echo the validated payload",
            "fixed_kwargs": {
                "argv": ["python", "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                "image": _IMAGE,
            },
            "input_schema": _INPUT_SCHEMA,
        },
    )
    assert created.status_code == 200, created.text

    async with sandbox_stack.mcp() as mcp:
        # A valid caller object is validated and routed into the base tool's payload_arg.
        marker = uniq("payload")
        valid = await mcp.call_tool(name, {"msg": marker})
        assert marker in valid.data.stdout, valid.data

        # An object violating the schema (wrong type / forbidden extra) is rejected LOUDLY as a
        # caller error — never routed raw into the base tool.
        bad_type = await mcp.call_tool(name, {"msg": 123}, raise_on_error=False)
        assert bad_type.is_error, bad_type
        extra = await mcp.call_tool(name, {"msg": "x", "unexpected": 1}, raise_on_error=False)
        assert extra.is_error, extra


async def test_input_schema_over_an_unsupported_base_is_a_loud_authoring_error(
    sandbox_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # Declaring an input_schema over a base tool that registered NO PresetInputSchemaSupport
    # (``e2e_echo``) is refused loudly at the authoring chokepoint — the mechanism never
    # silently ignores a schema.
    resp = await sandbox_stack.api().request_raw(
        "POST",
        "/api/presets",
        json={
            "name": uniq("unsupported"),
            "base_tool": "e2e_echo",
            "description": "echo takes no input_schema",
            "input_schema": _INPUT_SCHEMA,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "does not accept a preset input_schema" in resp.json()["error"], resp.text


# ---- the admin fence bites only where the platform fences ----------------


async def test_invocation_fence_is_a_no_op_with_access_control_off(
    sandbox_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # With access control OFF, ``resolve_caller`` returns an admin, so the base tool's own
    # direct-invocation admin fence is a no-op: a bare ``sandbox_exec`` edge call SUCCEEDS,
    # proving the fence bites only where the platform fences at all.
    token = uniq("fence")
    async with sandbox_stack.mcp() as mcp:
        result = await mcp.call_tool("sandbox_exec", {"argv": ["python", "-c", f"print({token!r})"], "image": _IMAGE})
    assert result.data.exit_code == 0, result.data
    assert token in result.data.stdout, result.data


# The AUTH-ON positive — authoring a ``sandbox_exec`` preset is admin-fenced (a non-admin editor
# DENIED, an admin ALLOWED) — has no reachable home here: authoring rides the generic ``presets:
# write`` action-class gate, which no auth-ON stack that ALSO registers a sandbox provider +
# ``sandbox_exec`` mounts. Every sandbox stack runs access control OFF, ``projection_authz_stack``
# (the auth-ON projection profile) wires no sandbox provider so a ``sandbox_exec`` base tool never
# registers on it, and ``fresh_stack`` cannot seed the pre-boot authz route table an auth-ON boot
# needs. Standing one up is a new manifest builder + a new pre-boot authz-seeding fixture
# (manifests.py / conftest foundation), out of scope for this suite. The mechanism itself is
# proven generically in ``tests/access_control/test_editable_role_levels.py`` as two halves of the
# SAME per-tag action-class gate: a ``presets: read`` holder reaches ``GET /api/presets`` but is
# DENIED ``POST /api/presets`` (403) — the read-denies-write half on the presets tag itself — while
# the write-ALLOWED half is proven on ``hooks: write`` (``POST /api/hooks`` -> 200). The gate is
# per-tag uniform across every base tool, so a ``presets: write`` grant reaching ``POST /api/presets``
# — the exact edge a ``sandbox_exec`` preset authoring rides — follows from those two proven halves.
@pytest.mark.skip(
    reason="the sandbox_exec preset admin-fence positive needs an auth-ON stack that also "
    "registers a sandbox provider + sandbox_exec; none exists and standing one up is a new "
    "manifest builder + pre-boot authz-seeding fixture (foundation), out of scope. The presets: "
    "write action-class gate it rides is proven generically in "
    "tests/access_control/test_editable_role_levels.py."
)
async def test_authoring_a_sandbox_exec_preset_is_admin_fenced() -> None:  # pragma: no cover - documented gap
    ...
