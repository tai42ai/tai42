"""Harness self-tests for the twilio and whatsapp stubs' automatic delivery receipts: off until a
door is set; then each accepted send is followed by the genuinely signed ``delivered`` status the
stub's own ``build_status`` makes, posted to the door; a withheld send gets none; a receipt the
door refuses is kept as a failure until drained."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from tai42_e2e import wait_absent_async, wait_for_async
from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.channel_stubs import FakeTwilio, FakeWhatsApp, ReceiptDoor
from tai42_e2e.ports import allocate_port

pytestmark = pytest.mark.needs("no-stack")

_SECRET = "receipt-secret"
_DOOR_PATH = "/status"


class _Door:
    """A status door that records every post and answers ``status_code``."""

    def __init__(self) -> None:
        self.posts: list[tuple[dict[str, str], bytes]] = []
        self.status_code = 200
        self.port = allocate_port()
        app = FastAPI()

        @app.post(_DOOR_PATH)
        async def status(request: Request) -> Response:
            self.posts.append((dict(request.headers), await request.body()))
            return Response(status_code=self.status_code)

        self._server = ThreadedServer(app, "127.0.0.1", self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{_DOOR_PATH}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()


@pytest.fixture
def door() -> Iterator[_Door]:
    door = _Door()
    door.start()
    try:
        yield door
    finally:
        door.stop()


@pytest.fixture
def whatsapp() -> Iterator[FakeWhatsApp]:
    stub = FakeWhatsApp()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def twilio() -> Iterator[FakeTwilio]:
    stub = FakeTwilio()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


async def _whatsapp_send(stub: FakeWhatsApp, body: str) -> str:
    async with httpx.AsyncClient(base_url=stub.api_base_url, timeout=5.0) as client:
        resp = await client.post(
            "/PN-1/messages",
            json={"messaging_product": "whatsapp", "to": "15550001", "type": "text", "text": {"body": body}},
        )
    assert resp.status_code == 200
    return resp.json()["messages"][0]["id"]


async def _twilio_send(stub: FakeTwilio, body: str) -> str:
    async with httpx.AsyncClient(base_url=stub.api_base_url, timeout=5.0) as client:
        resp = await client.post("/Accounts/AC1/Messages.json", data={"To": "+1", "From": "+2", "Body": body})
    assert resp.status_code == 201
    return resp.json()["sid"]


async def _posts(door: _Door, count: int) -> list[tuple[dict[str, str], bytes]]:
    async def arrived() -> list[tuple[dict[str, str], bytes]] | None:
        return door.posts if len(door.posts) >= count else None

    return await wait_for_async(arrived, deadline=5.0, message=f"fewer than {count} receipts reached the door")


async def _no_post(door: _Door) -> None:
    async def posted() -> list[tuple[dict[str, str], bytes]] | None:
        return door.posts or None

    await wait_absent_async(posted, window=0.5, interval=0.05, message="a receipt was posted")


async def test_whatsapp_posts_a_signed_delivered_status_for_each_send(whatsapp: FakeWhatsApp, door: _Door) -> None:
    await _whatsapp_send(whatsapp, "before the door")
    await _no_post(door)

    whatsapp.receipts.door = ReceiptDoor(door.url, _SECRET)
    wamid = await _whatsapp_send(whatsapp, "hello")

    ((headers, body),) = await _posts(door, 1)
    expected = whatsapp.build_status(
        app_secret=_SECRET, phone_number_id="PN-1", wamid=wamid, status="delivered", recipient_id="15550001"
    )
    assert body == expected.body
    assert headers["x-hub-signature-256"] == expected.headers["X-Hub-Signature-256"]
    assert whatsapp.receipts.drain_failures() == []


async def test_twilio_posts_a_signed_delivered_status_for_each_send(twilio: FakeTwilio, door: _Door) -> None:
    twilio.receipts.door = ReceiptDoor(door.url, _SECRET)
    sid = await _twilio_send(twilio, "hello")

    ((headers, body),) = await _posts(door, 1)
    expected = twilio.build_status(auth_token=_SECRET, public_url=door.url, message_sid=sid, status="delivered")
    assert body == expected.body
    assert headers["x-twilio-signature"] == expected.headers["X-Twilio-Signature"]
    assert twilio.receipts.drain_failures() == []


async def test_a_mark_read_gets_no_receipt(whatsapp: FakeWhatsApp, door: _Door) -> None:
    whatsapp.receipts.door = ReceiptDoor(door.url, _SECRET)
    async with httpx.AsyncClient(base_url=whatsapp.api_base_url, timeout=5.0) as client:
        resp = await client.post(
            "/PN-1/messages", json={"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.in"}
        )
    assert resp.status_code == 200
    await _no_post(door)


async def test_a_withheld_send_gets_no_receipt(whatsapp: FakeWhatsApp, door: _Door) -> None:
    whatsapp.receipts.door = ReceiptDoor(door.url, _SECRET)
    whatsapp.receipts.withhold("quiet-1")

    await _whatsapp_send(whatsapp, "a quiet-1 part")
    await _no_post(door)
    await _whatsapp_send(whatsapp, "a loud part")
    assert len(await _posts(door, 1)) == 1


async def test_a_refused_receipt_is_kept_until_drained_across_a_reset(twilio: FakeTwilio, door: _Door) -> None:
    door.status_code = 403
    twilio.receipts.door = ReceiptDoor(door.url, _SECRET)
    sid = await _twilio_send(twilio, "hello")
    twilio.reset()
    assert twilio.receipts.door is None

    async def drained() -> list[str] | None:
        return twilio.receipts.drain_failures() or None

    (failure,) = await wait_for_async(drained, deadline=5.0, message="the refused receipt was not kept")
    assert sid in failure
    assert "HTTP 403" in failure
    assert len(door.posts) == 1
    assert twilio.receipts.drain_failures() == []
