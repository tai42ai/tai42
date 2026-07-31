"""Outbound HTTP for the Slack channel — kit's pooled ``HttpxClient``.

Both outbound calls (``chat.postMessage`` and the loopback callback forward)
share one pooled ``httpx.AsyncClient`` per event loop, budgeted by
``CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS``. The Events API handler forwards the answer
before acking within Slack's 3 s window; a slower forward surfaces as a loud
handler failure that Slack's retry (plus the dedupe rollback in ``inbound``)
recovers.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

import httpx
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_slack.settings import slack_settings


def slack_http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A pooled outbound client budgeted by ``CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS``."""
    return tai42_app.clients.client_ctx(HttpxClient, timeout=slack_settings().http_timeout_seconds)
