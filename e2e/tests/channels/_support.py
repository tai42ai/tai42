"""Per-medium adapters + shared helpers for the channel cross-worker specs.

One ``channel_stack`` (REPLICAS, no backend, all three channel plugins loaded)
serves telegram + slack + twilio. Every spec parametrizes over the three via the
``channel_case`` fixture (in ``conftest.py``), which binds a :class:`ChannelCase`
to its recording stub and the live stack. The case owns everything
medium-specific: the recipient policy, how the plugin's one outbound send is read
back, how a GENUINELY-signed inbound is synthesized (so the plugin's real
signature verification runs, never a bypass), and the plugin's correlation-key
prefix.

This module is imported by both ``conftest.py`` (for the fixtures) and the spec
modules (``from _support import ...``); the test dir carries no package
``__init__``, so it is reached by its bare module name on the collected dir's
path — never as ``tests.channels.conftest`` (that name does not resolve here).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import redis as redis_lib
from fastmcp.client.client import CallToolResult

from tai42_e2e import manifests
from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio, SignedInbound
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# ---- MCP + interactions observation helpers -----------------------------


async def cancel_and_join(task: asyncio.Task) -> None:
    """Cancel a still-running ask/probe task and await it, so its ``async with
    stack.mcp(...)`` context unwinds promptly and no "task was destroyed while
    pending" / "exception was never retrieved" warning leaks into the failure
    output when an assertion trips before the task was awaited."""
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def await_true(fn: object, *, deadline: float, message: str):
    """Poll a plain synchronous predicate to a deadline, returning its first
    truthy value (a bridge over ``wait_for_async``, which awaits its predicate)."""

    async def probe():
        return fn()  # type: ignore[operator]

    return await wait_for_async(probe, deadline=deadline, message=message)


def tool_content_text(result: CallToolResult) -> str:
    """The full text of an MCP result's content blocks. On an error result a loud
    channel failure surfaces here through the public tool surface (the exception
    class does not survive MCP serialization; the stable message substring does);
    on a success result it carries the tool's return string."""
    return json.dumps([block.model_dump(mode="json") for block in (result.content or [])])


async def find_pending(stack: TaiStack, port: int, question: str, *, deadline: float = 8.0) -> str:
    """Stream the interactions backlog on ``port`` until a pending interaction
    whose payload carries ``question`` appears; return its id."""
    import httpx

    url = f"http://{stack.host}:{port}/api/interactions/stream"

    async def probe() -> str | None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            try:
                async with client.stream("GET", url) as response:
                    buffer = ""
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            for line in frame.splitlines():
                                if line.startswith("data:") and question in line:
                                    data = json.loads(line[len("data:") :].strip())
                                    return data.get("interaction_id") or data.get("id")
                            if "backlog_done" in frame:
                                return None
            except httpx.HTTPError:
                return None
        return None

    found = await wait_for_async(
        probe, deadline=deadline, message=f"pending interaction for {question!r} never appeared"
    )
    assert found is not None
    return found


async def is_pending(stack: TaiStack, port: int, question: str) -> bool:
    """Whether a pending interaction carrying ``question`` is in the backlog on
    ``port`` right now (one backlog read, no wait)."""
    import httpx

    url = f"http://{stack.host}:{port}/api/interactions/stream"
    async with httpx.AsyncClient(timeout=3.0) as client, client.stream("GET", url) as response:
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data:") and question in line:
                        return True
                if "backlog_done" in frame:
                    return False
    # The backlog stream ended without a ``backlog_done`` sentinel — a server-side
    # fault, not "not pending". Raise loudly rather than report a false absence
    # that a zero-pending assertion could then pass on vacuously.
    raise AssertionError(f"interactions stream on port {port} closed before backlog_done; pending state unknown")


def count_correlation_keys(stack: TaiStack, prefix: str) -> int:
    """Read-only count of the plugin's correlation keys in the stack's logical
    Redis DB — the census-style observation the correlation-absence asserts
    delta against (never a write)."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        return sum(1 for _ in client.scan_iter(match=f"{prefix}*"))
    finally:
        client.close()


def _callback_url_after_marker(text: str) -> str:
    """The bare URL a plain-link Tier-1 message carries after ``Answer here: ``."""
    for line in text.splitlines():
        if "Answer here: " in line:
            return line.split("Answer here: ", 1)[1].strip()
    raise AssertionError(f"no 'Answer here:' link in message text: {text!r}")


# ---- per-medium adapters ------------------------------------------------


class ChannelCase:
    """A medium bound to its recording stub and the live stack. Subclasses fill
    the medium-specific behavior; the class attributes carry the recipient policy
    the ``channel_stack`` env pins (kept in one place in ``manifests``)."""

    name: str = ""
    default_recipient: str = ""
    allowlisted_recipient: str = ""
    unlisted_recipient: str = ""
    allowlist_env: str = ""
    correlation_prefix: str = ""
    secret_env: str = ""
    # Every medium's inbound door answers one constant deny for a bad/missing
    # signature (the plan's fail-closed anchor).
    deny_status: int = 401

    def __init__(self, stack: TaiStack, stub: object) -> None:
        self.stack = stack
        self.stub = stub

    @property
    def inbound_path(self) -> str:
        return f"/api/channels/{self.name}/inbound"

    @property
    def callback_origin(self) -> str:
        """The origin the inbound door reconstructs a signed callback URL from.

        A mock leg reaches the door over the replica-B loopback origin, so the
        signature is computed over that. A real inbound leg fills the door's public
        base URL from ``E2E_PUBLIC_BASE_URL``; the vendor then signs over that
        public origin, so the verification must bind to it — same mechanism, new
        base.

        The public origin is bound ONLY when THIS case's OWN medium runs real —
        ``is_real(self.name)``. The stack-wide routing (``public_base_url_env_keys``
        carries ``INTERACTIONS_PUBLIC_BASE_URL`` whenever ANY channel seam is real)
        must NOT decide this: in a mixed session (e.g. real telegram alongside a
        mock twilio parametrization) the key is routed for the stack, yet the mock
        twilio door still reaches over loopback — gating on the routed key would make
        it sign over the public origin while POSTing to loopback, a 401. Nor does
        ``public_base_url`` alone decide it: the manifest carries the ambient
        ``E2E_PUBLIC_BASE_URL`` onto every leg (including mock). Gating on this
        medium's own real leg keeps a mock medium bound to loopback regardless of
        other real channels or of ``E2E_PUBLIC_BASE_URL``."""
        public = self.stack.config.public_base_url
        if public is not None and self.stack.infra.settings.is_real(self.name):
            return public.rstrip("/")
        return f"http://{self.stack.host}:{self.stack.port_b}"

    def assert_inbound_forwarded(self, response: object) -> None:
        """Assert the medium's inbound door acked the forwarded answer (each
        medium picks its own success status/shape)."""
        raise NotImplementedError

    @property
    def _secret(self) -> str:
        return self.stack.config.env[self.secret_env]

    def sends_matching(self, text: str) -> list[dict]:
        raise NotImplementedError

    def outbound_recipient(self, record: dict) -> str:
        raise NotImplementedError

    def assert_tier2_shape(self, record: dict) -> None:
        """Assert the delivered Tier-2 (text/select) message carries the medium's
        reply-correlation affordance."""
        raise NotImplementedError

    def assert_notify_plain(self, record: dict, message: str) -> None:
        """Assert a ``notify`` send carried exactly the message text with no
        reply-correlation affordance (fire-and-forget, nothing travels back)."""
        raise NotImplementedError

    def tier1_callback_url(self, record: dict) -> str:
        """The callback URL a Tier-1 (confirm/external) message carries."""
        raise NotImplementedError

    def build_reply(self, record: dict, answer: str, *, recipient: str, signature: str = "valid") -> SignedInbound:
        """A synthesized inbound reply to POST at replica B. ``signature`` is one
        of ``valid`` / ``wrong`` / ``missing`` (the fail-closed negatives)."""
        raise NotImplementedError


class TelegramCase(ChannelCase):
    name = "telegram"
    default_recipient = manifests.TELEGRAM_DEFAULT_RECIPIENT
    allowlisted_recipient = manifests.TELEGRAM_ALLOWED_RECIPIENTS[0]
    unlisted_recipient = manifests.TELEGRAM_UNLISTED_RECIPIENT
    allowlist_env = "CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"
    correlation_prefix = "channel:telegram:corr:"
    secret_env = "CHANNEL_TELEGRAM_WEBHOOK_SECRET"

    stub: FakeTelegram

    def sends_matching(self, text: str) -> list[dict]:
        return self.stub.sends_matching(text)

    def outbound_recipient(self, record: dict) -> str:
        return record["chat_id"]

    def assert_inbound_forwarded(self, response: object) -> None:
        assert response.status_code == 200  # type: ignore[attr-defined]
        assert response.json()["data"]["status"] == "forwarded"  # type: ignore[attr-defined]

    def assert_tier2_shape(self, record: dict) -> None:
        assert record["reply_markup"] == {"force_reply": True, "input_field_placeholder": "Reply to answer"}

    def assert_notify_plain(self, record: dict, message: str) -> None:
        assert record["text"] == message
        assert record["reply_markup"] is None

    def tier1_callback_url(self, record: dict) -> str:
        return record["reply_markup"]["inline_keyboard"][0][0]["url"]

    def build_reply(self, record: dict, answer: str, *, recipient: str, signature: str = "valid") -> SignedInbound:
        secret = {"valid": self._secret, "wrong": self._secret + "x", "missing": self._secret}[signature]
        inbound = self.stub.build_inbound(
            secret=secret, chat_id=recipient, reply_to_message_id=record["message_id"], text=answer
        )
        headers = dict(inbound.headers)
        if signature == "missing":
            headers.pop("X-Telegram-Bot-Api-Secret-Token")
        return SignedInbound(headers=headers, body=inbound.body)


class SlackCase(ChannelCase):
    name = "slack"
    default_recipient = manifests.SLACK_DEFAULT_RECIPIENT
    allowlisted_recipient = manifests.SLACK_ALLOWED_RECIPIENTS[0]
    unlisted_recipient = manifests.SLACK_UNLISTED_RECIPIENT
    allowlist_env = "CHANNEL_SLACK_ALLOWED_RECIPIENTS"
    correlation_prefix = "channel:slack:corr:"
    secret_env = "CHANNEL_SLACK_SIGNING_SECRET"

    stub: FakeSlack

    def sends_matching(self, text: str) -> list[dict]:
        return self.stub.sends_matching(text)

    def outbound_recipient(self, record: dict) -> str:
        return record["channel"]

    def assert_inbound_forwarded(self, response: object) -> None:
        assert response.status_code == 200  # type: ignore[attr-defined]
        assert response.json()["status"] == "forwarded"  # type: ignore[attr-defined]

    def assert_tier2_shape(self, record: dict) -> None:
        assert "Reply in this thread" in record["text"]

    def assert_notify_plain(self, record: dict, message: str) -> None:
        assert record["text"] == message

    def tier1_callback_url(self, record: dict) -> str:
        return _callback_url_after_marker(record["text"])

    def build_reply(self, record: dict, answer: str, *, recipient: str, signature: str = "valid") -> SignedInbound:
        inbound = self.stub.build_inbound(
            signing_secret=self._secret,
            channel=recipient,
            thread_ts=record["ts"],
            text=answer,
            event_id=f"Ev{uuid.uuid4().hex[:12]}",
            valid=signature != "wrong",
        )
        headers = dict(inbound.headers)
        if signature == "missing":
            headers.pop("X-Slack-Signature")
        return SignedInbound(headers=headers, body=inbound.body)


class TwilioCase(ChannelCase):
    name = "twilio"
    default_recipient = manifests.TWILIO_DEFAULT_RECIPIENT
    allowlisted_recipient = manifests.TWILIO_ALLOWED_RECIPIENTS[0]
    unlisted_recipient = manifests.TWILIO_UNLISTED_RECIPIENT
    allowlist_env = "CHANNEL_TWILIO_ALLOWED_RECIPIENTS"
    correlation_prefix = "channel:twilio:pending:"
    secret_env = "CHANNEL_TWILIO_AUTH_TOKEN"

    stub: FakeTwilio

    def sends_matching(self, text: str) -> list[dict]:
        return self.stub.sends_matching(text)

    def outbound_recipient(self, record: dict) -> str:
        return record["to"]

    def assert_inbound_forwarded(self, response: object) -> None:
        # Twilio's webhook contract wants a 2xx with no body; the door acks 204.
        assert response.status_code == 204  # type: ignore[attr-defined]

    def assert_tier2_shape(self, record: dict) -> None:
        # SMS has no reply-markup affordance — the number pair IS the correlation
        # key; the delivered body carries the question and no callback link.
        assert "Answer here:" not in record["body"]

    def assert_notify_plain(self, record: dict, message: str) -> None:
        assert record["body"] == message

    def tier1_callback_url(self, record: dict) -> str:
        return _callback_url_after_marker(record["body"])

    def build_reply(self, record: dict, answer: str, *, recipient: str, signature: str = "valid") -> SignedInbound:
        # The door reconstructs the signed URL from scheme + Host + path of the
        # request the test POSTs to — so the signature is computed over exactly the
        # origin the door binds to: replica-B loopback on a mock leg, the public
        # callback origin on a real inbound leg (``callback_origin``).
        public_url = f"{self.callback_origin}{self.inbound_path}"
        inbound = self.stub.build_inbound(
            auth_token=self._secret,
            public_url=public_url,
            twilio_number=self.stack.config.env["CHANNEL_TWILIO_FROM"],
            human_number=recipient,
            text=answer,
            valid=signature != "wrong",
        )
        headers = dict(inbound.headers)
        if signature == "missing":
            headers.pop("X-Twilio-Signature")
        return SignedInbound(headers=headers, body=inbound.body)


CASE_CLASSES: dict[str, type[ChannelCase]] = {
    "telegram": TelegramCase,
    "slack": SlackCase,
    "twilio": TwilioCase,
}


async def post_inbound(stack: TaiStack, path: str, inbound: SignedInbound):
    """POST a synthesized inbound to replica B's channel door — the reply arrives
    on the worker that never saw the ask. Returns the raw httpx response."""
    import httpx

    url = f"http://{stack.host}:{stack.port_b}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(url, content=inbound.body, headers=inbound.headers)


async def post_callback(url: str, body: bytes):
    """POST directly to a full callback URL (the Tier-1 confirm tap). Returns the
    raw httpx response."""
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(url, content=body)
