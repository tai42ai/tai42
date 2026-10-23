"""The address side of the ``Channel`` protocol, shared by the e2e stub channels.

Side-effect free on import: it registers no channel, so a stub module that reuses it
registers only its own.
"""

from __future__ import annotations

from tai42_contract.channels import ChannelAddress, ChannelDelivery, ChannelNotification, SendOutcome

# The identity and address a stub send resolves to when it names none of its own.
STUB_DEFAULT_IDENTITY = "e2e-stub-identity"
STUB_DEFAULT_RECIPIENT = "e2e-stub-recipient"


class StubAddressing:
    """The ``Channel`` protocol's address side, shared by the e2e stub channels.

    ``resolve_address`` answers the send's own recipient and identity, or the stub defaults when
    the send names none; a completed send reports the same pair through :meth:`_outcome`.
    """

    def resolve_address(self, recipient: str | None, sender_identity: str | None) -> ChannelAddress:
        return ChannelAddress(
            our_identity=sender_identity or STUB_DEFAULT_IDENTITY, address=recipient or STUB_DEFAULT_RECIPIENT
        )

    def _outcome(self, send: ChannelNotification | ChannelDelivery, message_ids: list[str]) -> SendOutcome:
        address = self.resolve_address(send.recipient, getattr(send, "sender_identity", None))
        return SendOutcome(our_identity=address.our_identity, address=address.address, message_ids=message_ids)
