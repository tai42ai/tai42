"""Fault-recovery probe: run the conversation delivery sweep once in this process.

The stalled-delivery sweep is a periodic in-process recovery loop with no HTTP or CLI
door, so a test cannot drive one pass on demand from outside the SUT. This probe runs
``sweep_stalled_deliveries`` through the skeleton's own seam inside the live worker: the
same pass the periodic loop runs, against the same conversations store the worker serves,
so a corrupt ``pending_delivery``/``provisional`` row a test seeded is moved to the
terminal ``failed`` state exactly as a scheduled sweep would move it.

Probes never mock — the effect is the SUT's own store transition."""

from __future__ import annotations

import os

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_sweep_stalled_deliveries() -> dict:
    """Run ONE stalled-delivery sweep pass in THIS process and report it ran.

    Drives the skeleton's ``sweep_stalled_deliveries`` against the conversations store
    this worker is wired to, so a test can observe the sweep's transitions (an
    unrecoverable row moved to terminal ``failed``) without waiting for the periodic
    loop's interval."""
    from tai42_skeleton.conversations.delivery import sweep_stalled_deliveries

    await sweep_stalled_deliveries()
    return {"swept": True, "pid": os.getpid()}


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_channel_inbound(
    channel: str,
    our_identity: str,
    client_address: str,
    text: str,
    provider_message_id: str,
) -> dict:
    """Accept one inbound message on ``channel`` IN THIS process and report its record id.

    A stub channel registers no inbound HTTP door, so a test cannot POST a webhook to it;
    this probe drives the same ``tai42_app.conversations.accept`` seam a real channel
    adapter calls, scheduling a bridged turn on the ``(channel, our_identity)`` route. The
    accountable per-address cap key is the ``client_address`` (a stub channel's attested
    address). The turn runs in the background; the accepted ``message_id`` is returned at once."""
    message_id = await tai42_app.conversations.accept(
        channel=channel,
        our_identity=our_identity,
        client_address=client_address,
        cap_key=client_address,
        text=text,
        provider_message_id=provider_message_id,
    )
    return {"message_id": message_id, "pid": os.getpid()}
