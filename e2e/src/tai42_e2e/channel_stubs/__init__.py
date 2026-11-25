"""Recording in-process stubs of the four channel providers' APIs, each a thread-
hosted FastAPI on an allocated loopback port. Each records the plugin's outbound
calls, mints the id the real provider would, and carries a helper that builds a
genuinely signed inbound request so the plugin's REAL signature verification runs
against it — never a bypass. Any unexpected path is a loud 500."""

from tai42_e2e.channel_stubs.media import HTML_BODY, TINY_PDF, TINY_PNG, MediaBlob
from tai42_e2e.channel_stubs.receipts import AutoReceipts, ReceiptDoor
from tai42_e2e.channel_stubs.slack import SLACK_PARTICIPANT_USER, FakeSlack
from tai42_e2e.channel_stubs.telegram import FakeTelegram
from tai42_e2e.channel_stubs.twilio import FakeTwilio
from tai42_e2e.channel_stubs.whatsapp import FakeWhatsApp, FlowExchange

__all__ = [
    "HTML_BODY",
    "SLACK_PARTICIPANT_USER",
    "TINY_PDF",
    "TINY_PNG",
    "AutoReceipts",
    "FakeSlack",
    "FakeTelegram",
    "FakeTwilio",
    "FakeWhatsApp",
    "FlowExchange",
    "MediaBlob",
    "ReceiptDoor",
]
