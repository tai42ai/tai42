"""A minimal deliver-only channel registered SUT-side on import.

Imported via a manifest ``channel_modules`` entry; on import it runs
``tai42_app.channels.register(...)`` exactly as a real channel plugin does, so a
stack can drive a channel-delivered ``ask`` without loading a real medium
plugin. Delivery is a no-op success (a plain return): the question is persisted
and its callback ticket minted before ``deliver`` is called, and the test bridges
the human's reply back by POSTing that ticket to the public callback door itself —
so the stub never has to reach an external medium.

Registering NO route keeps the fixture additive: it touches only the channel
registry, never the ``/api/*`` route table other stacks' gates enumerate.

Beside the deliver-only stub, two working-signal stubs prove the skeleton's
per-turn "working-on-it" refresh loop end to end: ``stub_working`` advertises a
short vendor-indicator lifetime so the loop refreshes it repeatedly over a long
turn, and ``stub_working_off`` advertises none so the loop never starts. Both
record every ``signal_working`` call onto a harness probe list a test reads back,
mirroring how :mod:`stub_form_channel` records a delivered form.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification
from tai42_kit.clients import RedisConnectionSettings

STUB_CHANNEL_NAME = "stub"

# The two working-signal stub channels: one with a vendor indicator (the loop
# refreshes it), one without (the loop never starts).
WORKING_SIGNAL_STUB_CHANNEL_NAME = "stub_working"
WORKING_SIGNAL_OFF_STUB_CHANNEL_NAME = "stub_working_off"

# The vendor-indicator lifetime the counting stub advertises, in seconds — short so a
# few-second turn crosses several refresh intervals.
WORKING_SIGNAL_STUB_EXPIRY_SECONDS = 2.0


class _StubChannel:
    """Satisfies the full ``Channel`` protocol. ``deliver`` succeeds without
    contacting any medium; ``notify`` raises, exactly as the contract prescribes
    for a channel without a notify capability.

    Advertises ``supports_form_delivery`` so a channel-delivered ``form`` ask (with
    per-send ``data``/``pages``) is accepted and its callback form page minted — the
    generic vehicle a core e2e uses to drive the form surface without a real medium
    plugin. It renders nothing itself: the human answers on the callback form page."""

    supports_form_delivery = True

    async def deliver(self, delivery: ChannelDelivery) -> None:
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        raise NotImplementedError


class _ProbeRedisSettings(RedisConnectionSettings):
    """Points the capture client at the harness probe channel via ``E2E_PROBE_REDIS_URL`` — inlined
    so importing this channel module never pulls in the probe TOOLS package (whose tool
    registrations are not in this stack's manifest)."""

    model_config = SettingsConfigDict(env_prefix="E2E_PROBE_")


def _working_signal_record_key(provider_message_id: str | None) -> str:
    """The probe list a ``signal_working`` call is recorded onto, keyed by the inbound id.

    Every refresh of one turn's indicator carries the same ``provider_message_id`` (the
    inbound message the turn answers), so a test reads all of a turn's calls off one list."""
    return f"e2e:rec:working_signal:{provider_message_id}"


class _CountingWorkingSignalChannel:
    """A channel that delivers a bridged answer and COUNTS every working-signal refresh.

    ``notify`` accepts the answer (a no-op success returning one opaque outbound id) so a
    bridged turn reaches ``provisional`` and the loop's first-send cancel fires; ``deliver`` is
    the no-op the ask path needs. ``signal_working`` RPUSHes ``{active, recipient,
    provider_message_id, sender_identity}`` onto the harness probe list so a spec proves the
    loop refreshed the indicator over the turn and stopped once the answer sent.

    ``working_signal_expiry_seconds`` is an instance attribute (``getattr`` reads it exactly as
    it reads the ``ClassVar`` a real channel declares), so one class serves both the
    vendor-indicator stub (a float lifetime) and the opt-out stub (``None`` — the loop never
    starts, and its ``signal_working`` is never called)."""

    def __init__(self, *, working_signal_expiry_seconds: float | None) -> None:
        self.working_signal_expiry_seconds = working_signal_expiry_seconds

    async def deliver(self, delivery: ChannelDelivery) -> None:
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        return [uuid4().hex]

    async def signal_working(
        self,
        *,
        recipient: str,
        sender_identity: str | None = None,
        provider_message_id: str | None = None,
        active: bool = True,
    ) -> None:
        from collections.abc import Awaitable
        from typing import cast

        from tai42_kit.clients import client_ctx
        from tai42_kit.clients.impl.redis import RedisClient

        record = json.dumps(
            {
                "active": active,
                "recipient": recipient,
                "provider_message_id": provider_message_id,
                "sender_identity": sender_identity,
                "pid": os.getpid(),
            }
        )
        async with client_ctx(RedisClient, _ProbeRedisSettings()) as client:
            await cast(Awaitable[int], client.rpush(_working_signal_record_key(provider_message_id), record))


tai42_app.channels.register(STUB_CHANNEL_NAME, _StubChannel())
tai42_app.channels.register(
    WORKING_SIGNAL_STUB_CHANNEL_NAME,
    _CountingWorkingSignalChannel(working_signal_expiry_seconds=WORKING_SIGNAL_STUB_EXPIRY_SECONDS),
)
tai42_app.channels.register(
    WORKING_SIGNAL_OFF_STUB_CHANNEL_NAME,
    _CountingWorkingSignalChannel(working_signal_expiry_seconds=None),
)
