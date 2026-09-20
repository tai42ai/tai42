"""A minimal deliver-only channel registered SUT-side on import.

Imported via a manifest ``channel_modules`` entry; on import it runs
``tai42_app.channels.register(...)`` exactly as a real channel plugin does, so a
stack can drive a channel-delivered ``ask_user`` without loading a real medium
plugin. Delivery is a no-op success (a plain return): the question is persisted
and its callback ticket minted before ``deliver`` is called, and the test bridges
the human's reply back by POSTing that ticket to the public callback door itself —
so the stub never has to reach an external medium.

Registering NO route keeps the fixture additive: it touches only the channel
registry, never the ``/api/*`` route table other stacks' gates enumerate.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification

STUB_CHANNEL_NAME = "stub"


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


tai42_app.channels.register(STUB_CHANNEL_NAME, _StubChannel())
