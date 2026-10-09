"""A re-park raised during an out-of-band drive joins its conversation thread's park index.

The detached drive (an answer, an expiry, a reaper redelivery) runs with no conversation turn
around it: the platform re-establishes only the run's stored delivery address. A run that asks
again inside that drive must still store its thread, for both route kinds, so a later thread
delete cascade-cancels the new park.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.interactions import (
    DeliveryAddress,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.tools import tool_call_frame

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import ask
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions.continuation import drive_and_deliver

from ._continuation_support import configure_interactions_store, make_wired

_THREAD = "relay:chat:+15550001111"


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


def _reparking_driver(monkeypatch, reparks: list[SuspendedInteraction]) -> None:
    """Stand in for the resumed driver: under the park's identity it asks AGAIN instead of finishing."""

    async def _drive(identity, fingerprint, tool, interaction_id, answer, *args: Any, **kwargs: Any):
        tool_token = set_resume_continuation_tool(tool)
        id_token = set_execution_identity(CallerIdentity(user_id=identity, execution_key_fingerprint=fingerprint))
        try:
            with tool_call_frame(continues_chain=("probe_driver",)):
                repark = await ask("and then?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
        finally:
            reset_execution_identity(id_token)
            reset_resume_continuation_tool(tool_token)
        assert isinstance(repark, SuspendedInteraction)
        reparks.append(repark)
        return repark

    monkeypatch.setattr(continuation_module, "_run_continuation", _drive)


async def _drive_detached(wired, address: DeliveryAddress) -> None:
    await drive_and_deliver(
        wired.store,
        identity="svc-key",
        fingerprint="fp-1",
        tool="probe_resume",
        interaction_id="i-first",
        answer="yes",
        park_context=None,
        park_asked_by=("probe_driver",),
        delivery=address,
        run_delivery_id="run-1",
        candidates=None,
        receives_outcome=False,
    )


async def test_a_tool_route_repark_during_a_detached_drive_joins_its_thread(monkeypatch, wired):
    reparks: list[SuspendedInteraction] = []
    _reparking_driver(monkeypatch, reparks)

    await _drive_detached(
        wired, DeliveryAddress(tool="probe_deliver", context={"route_name": "chat"}, thread_id=_THREAD)
    )

    assert [p.interaction_id for p in reparks] != []
    assert await wired.fake.smembers(wired.store.thread_parks_key(_THREAD)) == {reparks[0].interaction_id}


async def test_an_agent_route_repark_during_a_detached_drive_joins_its_thread(monkeypatch, wired):
    reparks: list[SuspendedInteraction] = []
    _reparking_driver(monkeypatch, reparks)

    await _drive_detached(
        wired, DeliveryAddress(tool="probe_agent_deliver", context={"message_id": "m-1"}, thread_id=_THREAD)
    )

    assert [p.interaction_id for p in reparks] != []
    assert await wired.fake.smembers(wired.store.thread_parks_key(_THREAD)) == {reparks[0].interaction_id}
