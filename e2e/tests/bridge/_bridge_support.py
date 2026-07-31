"""Shared helpers for the messaging-bridge cross-repo suite.

The bridge routes an inbound message — from an authed API caller or a registered channel
adapter — to an agent turn whose answer is durably stored and delivered back. Every leg
drives one seam of that path end to end over the live ``bridge_stack`` (access control ON,
the redis conversations backend, the twilio + whatsapp channel plugins on their
in-process stubs, the scripted LLM stub).

Named ``_bridge_support`` (not ``_support``) so its module basename never collides with the
channels suite's ``_support`` under pytest's prepend import mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import BRIDGE_TWILIO_FROM
from tai42_e2e.netfixtures import FakeTwilio, FakeWhatsApp, SignedInbound
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

TWILIO_INBOUND_PATH = "/api/channels/twilio/inbound"
TWILIO_STATUS_PATH = "/api/channels/twilio/status"
WHATSAPP_INBOUND_PATH = "/api/channels/whatsapp/inbound"

# The client-safe text a failed or denied turn delivers (mirrors the turn engine's constant);
# a leg asserting an error outcome matches the answer against this.
ERROR_ANSWER_TEXT = "Sorry, something went wrong handling your message. Please try again."
SLOW_DOWN_TEXT = "You are sending messages faster than I can answer. Please wait a moment and try again."


@dataclass
class BridgeHarness:
    """One booted bridge stack bound to its channel stubs, the scripted LLM, and the seeded
    root token — the handle every leg drives."""

    stack: TaiStack
    root_token: str
    fake_twilio: FakeTwilio
    fake_whatsapp: FakeWhatsApp
    llm_stub: LlmStub

    # -- credentials read back from the stack env -----------------------------

    @property
    def twilio_secret(self) -> str:
        return self.stack.config.env["CHANNEL_TWILIO_AUTH_TOKEN"]

    @property
    def whatsapp_secret(self) -> str:
        return self.stack.config.env["CHANNEL_WHATSAPP_APP_SECRET"]

    @property
    def whatsapp_verify_token(self) -> str:
        return self.stack.config.env["CHANNEL_WHATSAPP_VERIFY_TOKEN"]

    # -- HTTP + MCP clients ---------------------------------------------------

    def api(self, *, port: int | None = None, token: str | None = None) -> ApiClient:
        """An admin-API client for a replica (default B), authenticating as the seeded root
        unless ``token`` names another key."""
        client = self.stack.api(port or self.stack.port_b)
        return client if token is None else client.with_token(token)

    # -- key minting ----------------------------------------------------------

    async def mint_key(self, *, user_id: str, scopes: list[str]) -> str:
        """Mint an api key with ``scopes`` through the root client; returns the raw
        ``sk-`` token. The route binds it by its ``user_id`` (the turn runs AS that id)."""
        created = await self.api().post(
            "/api/auth/api-keys",
            json={"user_id": user_id, "description": "e2e bridge key", "scopes": scopes},
        )
        # The create route returns {api_key, key_fingerprint}; the raw token is api_key.
        raw = created["api_key"] if isinstance(created, dict) else created
        assert isinstance(raw, str), f"expected a raw token string, got {created!r}"
        assert raw.startswith("sk-")
        return raw

    async def set_key_scopes(self, user_id: str, scopes: list[str]) -> None:
        """Rewrite a key's scopes in place (the de-scope arm of the auto-revocation leg)."""
        await self.api().put(f"/api/auth/api-keys/{user_id}", json={"scopes": scopes})

    async def delete_key(self, user_id: str) -> None:
        await self.api().delete(f"/api/auth/api-keys/{user_id}")

    # -- route CRUD -----------------------------------------------------------

    async def create_channel_route(
        self,
        *,
        route_name: str,
        agent: str,
        execution_key: str,
        channel: str,
        our_identity: str,
        token: str | None = None,
        expect: int = 200,
    ) -> Any:
        return await self.api(token=token).post(
            f"/api/conversations/{route_name}",
            json={
                "door": "channel",
                "agent_name": agent,
                "execution_key": execution_key,
                "channel": channel,
                "our_identity": our_identity,
            },
            expect=expect,
        )

    async def create_api_route(
        self,
        *,
        route_name: str,
        agent: str,
        execution_key: str,
        callback_url: str,
        token: str | None = None,
        expect: int = 200,
    ) -> Any:
        return await self.api(token=token).post(
            f"/api/conversations/{route_name}",
            json={
                "door": "api",
                "agent_name": agent,
                "execution_key": execution_key,
                "callback_url": callback_url,
            },
            expect=expect,
        )

    async def get_record(self, route_name: str, message_id: str, *, token: str | None = None, expect: int = 200) -> Any:
        return await self.api(token=token).get(f"/api/conversations/{route_name}/messages/{message_id}", expect=expect)

    async def list_failed(self, *, token: str | None = None, expect: int = 200) -> Any:
        return await self.api(token=token).get("/api/conversations/messages/failed", expect=expect)

    # -- channel inbound synthesis --------------------------------------------

    def twilio_inbound(
        self, *, our_identity: str, client: str, text: str, valid: bool = True, port: int | None = None
    ) -> SignedInbound:
        """A genuinely-signed uncorrelated twilio inbound: From ``client`` to the
        deployment number ``our_identity``. The signed URL is the exact door URL on the
        replica the reply is POSTed to."""
        public_url = f"http://{self.stack.host}:{port or self.stack.port_b}{TWILIO_INBOUND_PATH}"
        return self.fake_twilio.build_inbound(
            auth_token=self.twilio_secret,
            public_url=public_url,
            twilio_number=our_identity,
            human_number=client,
            text=text,
            valid=valid,
        )

    def twilio_status(
        self, *, message_sid: str, status: str, valid: bool = True, port: int | None = None
    ) -> SignedInbound:
        public_url = f"http://{self.stack.host}:{port or self.stack.port_b}{TWILIO_STATUS_PATH}"
        return self.fake_twilio.build_status(
            auth_token=self.twilio_secret, public_url=public_url, message_sid=message_sid, status=status, valid=valid
        )

    def whatsapp_inbound(
        self,
        *,
        phone_number_id: str,
        wa_id: str,
        text: str,
        wamid: str | None = None,
        valid: bool = True,
    ) -> SignedInbound:
        return self.fake_whatsapp.build_inbound(
            app_secret=self.whatsapp_secret,
            phone_number_id=phone_number_id,
            wa_id=wa_id,
            text=text,
            wamid=wamid,
            valid=valid,
        )

    def whatsapp_interactive_reply(
        self,
        *,
        phone_number_id: str,
        wa_id: str,
        reply_kind: str,
        reply_id: str,
        title: str,
        wamid: str | None = None,
        valid: bool = True,
    ) -> SignedInbound:
        """A genuinely-signed interactive reply (a tapped button or a picked list row)
        from ``wa_id`` to the ``phone_number_id`` the ask was sent FROM. ``reply_id`` is
        the question-bound id the outbound carried; ``title`` the option's visible label."""
        return self.fake_whatsapp.build_interactive_reply(
            app_secret=self.whatsapp_secret,
            phone_number_id=phone_number_id,
            wa_id=wa_id,
            reply_kind=reply_kind,
            reply_id=reply_id,
            title=title,
            wamid=wamid,
            valid=valid,
        )

    def whatsapp_status(
        self, *, phone_number_id: str, wamid: str, status: str, recipient_id: str = "", valid: bool = True
    ) -> SignedInbound:
        return self.fake_whatsapp.build_status(
            app_secret=self.whatsapp_secret,
            phone_number_id=phone_number_id,
            wamid=wamid,
            status=status,
            recipient_id=recipient_id,
            valid=valid,
        )


async def post_inbound(stack: TaiStack, path: str, inbound: SignedInbound, *, port: int | None = None) -> Any:
    """POST a synthesized inbound to a replica's channel door (default B). Returns the raw
    httpx response."""
    import httpx

    url = f"http://{stack.host}:{port or stack.port_b}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(url, content=inbound.body, headers=inbound.headers)


async def whatsapp_get_verify(stack: TaiStack, params: dict[str, str], *, port: int | None = None) -> Any:
    """GET the whatsapp inbound door with Meta's subscription-handshake params. Returns
    the raw httpx response (200 echoes ``hub.challenge`` in plaintext; a mismatch is 403)."""
    import httpx

    url = f"http://{stack.host}:{port or stack.port_b}{WHATSAPP_INBOUND_PATH}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, params=params)


async def wait_twilio_send(fake: FakeTwilio, text: str, *, deadline: float = 12.0) -> dict:
    """Wait until exactly one twilio outbound send carrying ``text`` is recorded; return
    it. The bridge send runs in a background task after the inbound door acks."""

    async def probe() -> list[dict]:
        return fake.sends_matching(text)

    records = await wait_for_async(probe, deadline=deadline, message=f"no twilio send carrying {text!r} was recorded")
    assert len(records) == 1, f"expected exactly one twilio send carrying {text!r}, saw {len(records)}"
    return records[0]


async def wait_whatsapp_send(fake: FakeWhatsApp, text: str, *, deadline: float = 12.0) -> dict:
    async def probe() -> list[dict]:
        return fake.sends_matching(text)

    records = await wait_for_async(probe, deadline=deadline, message=f"no whatsapp send carrying {text!r} was recorded")
    assert len(records) == 1, f"expected exactly one whatsapp send carrying {text!r}, saw {len(records)}"
    return records[0]


async def wait_channel_send_count(fake: Any, text: str, count: int, *, deadline: float = 30.0) -> list[dict]:
    """Wait until ``count`` outbound sends carrying ``text`` are recorded on ``fake`` (a twilio
    or whatsapp stub); assert it settled at exactly ``count`` and return them."""

    async def probe() -> list[dict] | None:
        matches = fake.sends_matching(text)
        return matches if len(matches) >= count else None

    matches = await wait_for_async(
        probe, deadline=deadline, message=f"fewer than {count} sends carrying {text!r} were recorded"
    )
    assert len(matches) == count, f"expected exactly {count} sends carrying {text!r}, saw {len(matches)}"
    return matches


async def wait_record_status(
    bridge: BridgeHarness, route_name: str, message_id: str, statuses: set[str], *, deadline: float = 12.0
) -> dict:
    """Poll the admin record read until the record's ``delivery_status`` is one of
    ``statuses``; return the record view."""

    async def probe() -> dict | None:
        record = await bridge.get_record(route_name, message_id)
        return record if record.get("delivery_status") in statuses else None

    return await wait_for_async(probe, deadline=deadline, message=f"record {message_id} never reached {statuses}")


async def drive_status_to_failed(
    bridge: BridgeHarness,
    post_status: Callable[[], Awaitable[Any]],
    outbound_id: str,
    *,
    deadline: float = 15.0,
) -> dict:
    """Post a signed ``failed`` status webhook until the record naming ``outbound_id`` flips
    terminal and surfaces on the admin failed listing; return that record view.

    Retried because a status can arrive in the sub-second window before the send's record
    reaches ``provisional``, where the receipt is refused; the repeat is idempotent once the
    record has flipped."""

    async def probe() -> dict | None:
        await post_status()
        failed = await bridge.list_failed()
        for item in failed["items"]:
            if outbound_id in item.get("outbound_message_ids", []):
                return item
        return None

    return await wait_for_async(probe, deadline=deadline, message=f"outbound {outbound_id!r} never flipped to failed")


def tool_text(result: Any) -> str:
    """The full text of an MCP tool result's content blocks. A loud operation/channel
    failure surfaces its stable message substring here on an error result (the exception
    class does not survive MCP serialization); a success result carries the return string."""
    return json.dumps([block.model_dump(mode="json") for block in (result.content or [])])


def script_reply(llm_stub: LlmStub, *contents: str) -> None:
    """Script the LLM stub with one final-message turn per ``contents`` entry (each a whole
    agent turn's answer). Replaces the queue."""
    llm_stub.script([{"content": text} for text in contents])


def request_mentions(llm_stub: LlmStub, index: int, needle: str) -> bool:
    """Whether the ``index``-th recorded LLM request carried ``needle`` anywhere — the
    checkpoint-continuity proof (a later turn's request replays the earlier turn's history)."""
    requests = llm_stub.requests
    if index >= len(requests):
        return False
    return needle in json.dumps(requests[index])


def default_twilio_identity() -> str:
    """The deployment twilio number a single-route leg binds its route to."""
    return BRIDGE_TWILIO_FROM


async def cancel_and_join(task: asyncio.Task) -> None:
    """Cancel a still-running ask/turn task and await it so its context unwinds without a
    "task was destroyed while pending" warning when an assertion trips early."""
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def mint_execution_key(bridge: BridgeHarness, uniq: Callable[[str], str], *, scopes: list[str]) -> str:
    """Mint an execution key and return its ``user_id`` (what a route binds); the raw token
    is discarded — a tokenless fire reads the key's live grants by id."""
    user_id = uniq("execkey")
    await bridge.mint_key(user_id=user_id, scopes=scopes)
    return user_id
