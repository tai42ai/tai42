"""The states feature is OFF (501), never a boot abort, when its database is unbound.

The ``states`` component auto-binds ``default`` (like ``skeleton``), so a deployment with
no Postgres must NOT have the boot-time schema gate raise and abort startup — the gate is a
no-op while the component is unconfigured, and every door refuses 501
``states-not-configured``."""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import StatesNotConfiguredError
from tai42_contract.states.models import StateSubject
from tests._module_identity import restore_states_module_identity

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.states.service.service import StatesService


def test_boot_with_no_postgres_and_states_refuses_501() -> None:
    # No database password is set here, so the states component is unconfigured. The app
    # must boot without a startup error, and every states door must refuse 501.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            with pytest.raises(StatesNotConfiguredError):
                await tai42_app.states.read(
                    "alerts", StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
                )

    asyncio.run(run())


def test_states_gate_ignores_an_orphaned_service_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate resolves ``states_store_configured`` through the ``states.service`` PARENT-package
    attribute. A suite that pops ``states.service`` from ``sys.modules``, re-imports it (rebinding
    the parent attribute to a fresh module) and restores the ``sys.modules`` entry by hand leaves
    the parent attribute pointing at the orphan. If that orphan carries a stale value, it would
    answer the gate for an unrelated later test. Restoring the states package's canonical
    submodules puts the live module — not the orphan — back in front of the gate."""
    service_name = "tai42_skeleton.states.service"
    states_pkg = sys.modules["tai42_skeleton.states"]
    live = sys.modules[service_name]
    # The live module is unconfigured (as it is with no database bound).
    monkeypatch.setattr(live, "states_store_configured", lambda: False)

    saved = sys.modules.pop(service_name)
    try:
        orphan = importlib.import_module(service_name)  # rebinds states_pkg.service -> orphan
        sys.modules[service_name] = saved  # sys.modules back to the live module; parent attr still orphan
        # the stale value the orphan would answer the gate with
        orphan.__dict__["states_store_configured"] = lambda: True

        # Bug condition: the parent attribute diverges and the gate reads the orphan's True.
        assert states_pkg.service is orphan
        assert states_pkg.service is not sys.modules[service_name]
        StatesService._ensure_available()  # reads the orphan -> wrongly passes

        # The restore puts the canonical module back; the gate now reads the live (unconfigured) one.
        restore_states_module_identity()
        assert states_pkg.service is sys.modules[service_name] is live
        with pytest.raises(StatesNotConfiguredError):
            StatesService._ensure_available()
    finally:
        restore_states_module_identity()
