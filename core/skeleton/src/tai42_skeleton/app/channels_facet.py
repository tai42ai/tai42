"""The app's channels facet — the ``app.channels`` namespace, in its own module
like ``app.clients`` (``app/clients.py``).

Forwards to the app's :class:`~tai42_skeleton.channels.registry.ChannelRegistry`.
A channel plugin registers a named deliverer here via an import-only
``channel_modules`` manifest entry; the ``ask_user`` helper resolves it by name
at ask time, and the channels catalog route lists the registered names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_contract.channels import Channel, CorrelationStore, InboundAnswerResult, InboundBridge

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP


class ChannelsFacet:
    """``app.channels`` — channel registration + lookup + the inbound-answer ladder
    (``AppChannels``)."""

    __slots__ = ("_app",)

    def __init__(self, app: TaiMCP) -> None:
        self._app = app

    def register(self, name: str, channel: Channel) -> None:
        return self._app._channel_registry.register(name, channel)

    def get(self, name: str) -> Channel:
        return self._app._channel_registry.get(name)

    def names(self) -> list[str]:
        return self._app._channel_registry.names()

    async def handle_inbound_answer(
        self,
        *,
        channel_id: str,
        correlation_key: str,
        answer: Any,
        store: CorrelationStore,
        bridge: InboundBridge,
    ) -> InboundAnswerResult:
        """The ONE shared inbound-answer ladder (see :meth:`AppChannels.handle_inbound_answer`).

        The policy lives in :mod:`tai42_skeleton.channels.inbound`; this facet is the
        contract-level seam channel plugins reach it through (a channel never imports
        the skeleton). Imported locally so the facet's load-time surface stays the
        registry — the ladder pulls the hooks manager and settings.
        """
        from tai42_skeleton.channels.inbound import handle_inbound_answer

        return await handle_inbound_answer(
            channel_id=channel_id,
            correlation_key=correlation_key,
            answer=answer,
            store=store,
            bridge=bridge,
        )
