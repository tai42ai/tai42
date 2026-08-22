"""The sandbox KIND doors: identity/policy present-regardless-of-provider, the
kind-status row in both states, the authed-door 401, and the consumer acquisition
error path.

The sandbox is a SCALAR-slot kind like the backend, not a DB-gated feature: its
absence doctrine is the backend's (``present: false`` with the resolved policy still
present, never a 501), so the doors here mirror ``test_backend_absent_is_honest``.
Every stack this module drives runs no backend and the sandbox surface is
backend-invariant, so it is ``backendless``.

The identity door (``GET /api/sandbox``) and the kind-status row read the SAME
operator-resolved :class:`SandboxPolicy` whether or not a provider is registered — the
four knobs live on ``CoreSettings``, which exist without a provider — so the resolved
``policy`` block is asserted present on BOTH the provider-less and the provider-backed
door. The consumer acquisition chokepoint (``require_sandbox``) raises a typed loud
``SandboxUnavailableError`` through a tool's own error path on a provider-less stack —
never a 501 route, because the sandbox has no mutation door.
"""

from __future__ import annotations

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import build_sandbox_stack
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

# The four platform-owned knobs the resolved policy envelope carries — present on the
# door regardless of whether a provider backs the slot.
_POLICY_FIELDS = frozenset({"egress", "isolation", "scrub_transcript", "durable"})

# The fixture provider that backs ``build_sandbox_stack`` — the module the active
# identity/row names.
_FAKE_PROVIDER_MODULE = "tai42_e2e_fixtures.sandbox_provider"


def _sandbox_stack_with_kind_status(res, variants):
    """``build_sandbox_stack`` plus the ``system_kinds`` router so the ACTIVE sandbox
    kind-status row is reachable (the router is not in the core set the base stack mounts)."""
    config = build_sandbox_stack(res, variants)
    config.manifest["routers_modules"] = [
        *config.manifest["routers_modules"],
        "tai42_skeleton.routers.system_kinds",
    ]
    return config


# ---- identity + policy, present regardless of provider -------------------


async def test_identity_door_absent_is_honest_and_carries_policy(bare_stack: TaiStack) -> None:
    # No provider registered: the door reports the absence truthfully (never errors, never
    # a 501) — and the resolved policy block is present ALONGSIDE ``present: false``.
    info = await bare_stack.api().get("/api/sandbox")
    assert info["present"] is False, info
    assert info["provider"] is None, info
    assert info["module"] is None, info
    assert info["sessions"] == 0, info
    assert info["policy"].keys() >= _POLICY_FIELDS, info


async def test_identity_door_present_names_the_provider_and_carries_policy(sandbox_stack: TaiStack) -> None:
    # A provider is registered: the door names it and still carries the SAME resolved
    # policy block (the operator's security-as-config, independent of the provider).
    info = await sandbox_stack.api().get("/api/sandbox")
    assert info["present"] is True, info
    assert info["module"] == _FAKE_PROVIDER_MODULE, info
    assert info["provider"], info
    assert info["policy"].keys() >= _POLICY_FIELDS, info


# ---- kind-status row, both states ----------------------------------------


async def test_kind_status_row_is_off_without_a_provider(off_stack: TaiStack) -> None:
    rows = await off_stack.api().get("/api/system/kinds")
    row = next((r for r in rows if r["kind"] == "sandbox"), None)
    assert row is not None, rows
    assert row["state"] == "off", row
    assert row["plugin"] is None, row
    assert row["detail"] == "no sandbox provider installed", row


async def test_kind_status_row_is_active_with_a_provider(fresh_stack) -> None:
    stack: TaiStack = fresh_stack(_sandbox_stack_with_kind_status)
    rows = await stack.api().get("/api/system/kinds")
    row = next((r for r in rows if r["kind"] == "sandbox"), None)
    assert row is not None, rows
    assert row["state"] == "active", row
    assert row["plugin"] == _FAKE_PROVIDER_MODULE, row
    # The row carries the introspection shape every kind row does.
    assert set(row) >= {"kind", "state", "plugin", "detail"}, row


# ---- authed read: 401 without a token ------------------------------------


async def test_identity_door_is_authed(projection_authz_stack) -> None:
    # The identity door is an AUTHED read: on an access-control stack an unauthenticated
    # GET is refused 401 — the door answers regardless of whether a provider is registered
    # (this profile registers none), so the refusal is the auth layer's, not an absent-kind
    # signal.
    stack, _root_token, _limited_token = projection_authz_stack
    anon = ApiClient(f"http://{stack.host}:{stack.port_a}")
    resp = await anon.request_raw("GET", "/api/sandbox")
    assert resp.status_code == 401, resp.text


# ---- consumer acquisition error path -------------------------------------


async def test_consumer_acquisition_raises_loudly_without_a_provider(bare_stack: TaiStack) -> None:
    # A consumer acquiring a session on a provider-less stack surfaces the typed loud
    # ``SandboxUnavailableError`` through the probe tool's OWN error path — never a 501
    # route (the sandbox has no mutation door). The message names the selecting knob.
    async with bare_stack.mcp() as mcp:
        result = await mcp.call_tool("e2e_sandbox_probe", {"op": "create"}, raise_on_error=False)
    assert result.is_error, result
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert "no sandbox provider is registered" in text, text
    assert "sandbox_module" in text, text
