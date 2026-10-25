"""Channels operations — the authed catalog read.

``list_channels`` returns the registered channels, each with its declared
identity hint: the delivery media ``ask(channel=...)`` can resolve and the
channels a conversation route can name. Registration is import-only (a manifest
``channel_modules`` entry); this operation is read-only.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.channels.registry import declared_identity_hint
from tai42_skeleton.operations import operation
from tai42_skeleton.operations.response_models_group_c import ChannelListing


@operation(summary="List registered channels", tags=["channels"], response_model=ChannelListing)
async def list_channels() -> dict:
    """The registered channels, sorted by name, for ``ask(channel=...)`` and route authoring.

    Each entry carries the channel's ``identity_hint`` for a route's ``our_identity`` (``None``
    when the channel declares none).
    """
    return {
        "channels": [
            {"name": name, "identity_hint": declared_identity_hint(name, tai42_app.channels.get(name))}
            for name in tai42_app.channels.names()
        ]
    }
