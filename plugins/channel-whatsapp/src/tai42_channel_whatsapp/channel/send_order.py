"""The order of the messages one ``notify`` or ``deliver`` sends.

Meta does not keep the order of a series of API requests; a sender that needs it confirms the
``delivered`` status of one message before sending the next.
https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
"""

from __future__ import annotations

from collections.abc import Sequence

from tai42_contract.app import tai42_app

from tai42_channel_whatsapp.client import SentMessage


async def await_previous_delivery(sent: Sequence[SentMessage]) -> None:
    """Hold the next send until the medium reports the last message of ``sent`` delivered.

    Nothing sent yet holds nothing. A FAILED receipt for that message raises
    ``ChannelDeliveryError``, so the rest of the sequence is not sent.
    """
    if sent:
        await tai42_app.conversations.await_delivery("whatsapp", sent[-1].wamid)
