"""The ``sandbox_exec`` base tool: its load-time preset-mechanism declarations and its
own DIRECT-invocation admin fence.

Imported here with the bound (unstarted) app, so the module's load-time
``register_input_schema_support`` / ``register_registration_tier`` calls land on the
app's registries and the tool function is callable directly for the fence checks.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import SandboxUnavailableError

from tai42_skeleton.app.instance import build_app
from tai42_skeleton.operations.errors import ForbiddenError
from tai42_skeleton.tools.builtin import sandbox_exec as se
from tai42_skeleton.tools.reveal_gate import InprocessRevealGate, inprocess_reveal_gate


@dataclass
class _Caller:
    is_admin: bool


def test_declares_input_schema_support_and_fenced_tier_at_load() -> None:
    # Import-order-independent: the two per-base-tool registries are process-shared and
    # RESET on every app_context boot, so a prior test's boot may have wiped the module's
    # first-import declaration. Re-run the module's load against a clean, manifest-less app
    # (the tool decorator no-ops with no manifest; only the two preset declarations run) so
    # the assertion sees the module's OWN declared values regardless of suite ordering.
    app = build_app()
    with tai42_app.bound(app):
        app._manifest = None
        app._input_schema_support_registry.reset()
        app._registration_tier_registry.reset()
        sys.modules.pop("tai42_skeleton.tools.builtin.sandbox_exec", None)
        importlib.import_module("tai42_skeleton.tools.builtin.sandbox_exec")
        support = tai42_app.presets.input_schema_support("sandbox_exec")
        assert support is not None
        assert support.payload_arg == "input"
        assert tai42_app.presets.registration_tier("sandbox_exec") == "fenced"


async def test_direct_non_admin_call_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gate UNARMED (a direct edge call) + a non-admin caller → refused before touching
    # the sandbox, so the authoring fence cannot be bypassed by a direct invocation.
    async def _caller() -> _Caller:
        return _Caller(is_admin=False)

    monkeypatch.setattr(se, "resolve_caller", _caller)
    with pytest.raises(ForbiddenError):
        await se.sandbox_exec(["echo"], image="img")


async def test_direct_admin_call_passes_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gate UNARMED + an admin caller → the fence passes; with no provider registered the
    # acquisition chokepoint then raises loudly (proving the fence let it through).
    async def _caller() -> _Caller:
        return _Caller(is_admin=True)

    monkeypatch.setattr(se, "resolve_caller", _caller)
    with pytest.raises(SandboxUnavailableError):
        await se.sandbox_exec(["echo"], image="img")


async def test_preset_forwarded_call_skips_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gate ARMED (a preset's in-process forward) → the fence is skipped even for a
    # non-admin (its authoring was already admin-fenced). ``resolve_caller`` must NOT be
    # consulted; the acquisition chokepoint raises loudly, proving the fence let it through.
    def _fail() -> _Caller:
        raise AssertionError("resolve_caller must not be called when the reveal gate is armed")

    monkeypatch.setattr(se, "resolve_caller", _fail)
    token = inprocess_reveal_gate.set(InprocessRevealGate())
    try:
        with pytest.raises(SandboxUnavailableError):
            await se.sandbox_exec(["echo"], image="img")
    finally:
        inprocess_reveal_gate.reset(token)
