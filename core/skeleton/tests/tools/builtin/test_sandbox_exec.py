"""The ``sandbox_exec`` base tool: its load-time tool-mechanism declarations.

Imported here with the bound (unstarted) app so the module's load-time
``register_input_schema_support`` / ``register_tier`` calls land on the app's registries.
The run-time admin fence is generic — enforced at the tool-run chokepoint, not inside the
tool — so it is tested at that seam (``tests/tools/test_run_tier_fence.py``), never here.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import SandboxUnavailableError

from tai42_skeleton.app.instance import build_app
from tai42_skeleton.tools.builtin import sandbox_exec as se

_IMAGE = "img@sha256:" + "0" * 64


def test_declares_input_schema_support_and_fenced_tier_at_load() -> None:
    # Import-order-independent: the two per-base-tool registries are process-shared and
    # RESET on every app_context boot, so a prior test's boot may have wiped the module's
    # first-import declaration. Re-run the module's load against a clean, manifest-less app
    # (the tool decorator no-ops with no manifest; only the two declarations run) so the
    # assertion sees the module's OWN declared values regardless of suite ordering.
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
        # The tier is declared through the tools facet and read back through both facet
        # names — the SAME shared registry.
        assert tai42_app.tools.tier("sandbox_exec") == "fenced"
        assert tai42_app.presets.registration_tier("sandbox_exec") == "fenced"


async def test_sandbox_exec_is_loud_without_a_provider() -> None:
    # The tool builds no policy of its own: with no provider registered on the bare app, the
    # acquisition chokepoint raises loudly rather than silently degrading. (The run-time
    # tier fence is a separate, generic concern at the tool-run seam.)
    with pytest.raises(SandboxUnavailableError):
        await se.sandbox_exec(["echo"], image=_IMAGE)
