"""The states feature is OFF (501), never a boot abort, when its database is unbound.

The ``states`` component auto-binds ``default`` (like ``skeleton``), so a deployment with
no Postgres must NOT have the boot-time schema gate raise and abort startup — the gate is a
no-op while the component is unconfigured, and every door refuses 501
``states-not-configured``."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import StatesNotConfiguredError
from tai42_contract.states.models import StateSubject

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest


def test_boot_with_no_postgres_and_states_refuses_501(monkeypatch: pytest.MonkeyPatch) -> None:
    # No database password is set here, so the states component is unconfigured. The app
    # must boot without a startup error, and every states door must refuse 501.
    # Guarantee that documented precondition rather than trusting the ambient env: a crashed
    # xdist worker could otherwise leave a sibling test's password/binding set, which this
    # live-env gate would then read as configured.
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    monkeypatch.delenv("TAI_DB_BINDING_STATES", raising=False)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            try:
                await tai42_app.states.read(
                    "alerts", StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
                )
            except StatesNotConfiguredError:
                pass
            else:  # pragma: no cover - the gate must refuse
                raise AssertionError("states.read did not refuse 501 while the store is unbound")

    asyncio.run(run())
