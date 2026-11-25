"""A recording stub of the Twilio REST Messages API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.channel_stubs.media import MediaBlob, serve_blob
from tai42_e2e.channel_stubs.receipts import AutoReceipts
from tai42_e2e.ports import allocate_port
from tai42_e2e.provider_stub import SignedInbound, _install_catch_all


class FakeTwilio:
    """A recording stub of the Twilio REST Messages API.

    Serves ``/Accounts/{sid}/Messages.json`` (minting a ``MessageSid``) and, once
    ``receipts.door`` is set, posts a signed ``delivered`` status for each accepted message
    (:class:`AutoReceipts`). Any other path answers a loud 500. Twilio has no boot-time API call (the number's
    inbound webhook is configured out-of-band), so nothing is served for
    startup."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.messages: list[dict[str, Any]] = []
        # An optional async hook awaited inside the send handler after a message is recorded and
        # before its response returns. A test that must interleave with a send in flight (the
        # receipt-race reproduction: hold one chunk in ``pending_delivery`` while the receipt for
        # an earlier chunk is posted) sets it; the default ``None`` leaves the handler unchanged.
        self.send_barrier: Callable[[], Awaitable[None]] | None = None
        self.receipts = AutoReceipts()
        # The SECOND origin, on its own loopback port — the CDN a ``MediaUrl`` 307s to. Serving the
        # bytes from a DIFFERENT origin is what makes the fetch cross-origin, so the kit drops the
        # HTTP Basic auth on the redirect hop; the CDN records the ``Authorization`` it saw per
        # fetch (``cdn_authorizations``) so a test proves the credential did NOT ride to it.
        self.cdn_port = allocate_port()
        self.cdn_authorizations: list[str | None] = []
        # Per-media-reference byte-serving behaviour (served by the CDN), keyed by the ref a test
        # builds through :meth:`media_url`; ``_media_served`` counts fetches for ``fail_times``.
        self.media: dict[str, MediaBlob] = {}
        self._media_served: dict[str, int] = {}
        self._server = ThreadedServer(self._build_app(), host, self.port)
        self._cdn_server = ThreadedServer(self._build_cdn_app(), host, self.cdn_port)

    @property
    def api_base_url(self) -> str:
        """The value ``CHANNEL_TWILIO_API_BASE_URL`` points at (the plugin
        addresses ``{api_base_url}/Accounts/{AccountSid}/Messages.json``)."""
        return f"http://{self.host}:{self.port}"

    def media_url(self, ref: str) -> str:
        """A ``MediaUrl`` on this stub for media reference ``ref`` — a Basic-authed GET that 307s
        to the CDN origin (a different port), where the bytes are served."""
        return f"http://{self.host}:{self.port}/media/{ref}"

    def start(self) -> None:
        self._server.start()
        self._cdn_server.start()

    def stop(self) -> None:
        self._cdn_server.stop()
        self._server.stop()

    def reset(self) -> None:
        self.messages.clear()
        self.send_barrier = None
        self.receipts.reset()
        self.media.clear()
        self._media_served.clear()
        self.cdn_authorizations.clear()

    def sends_matching(self, text: str) -> list[dict[str, Any]]:
        return [record for record in self.messages if text in record["body"]]

    def build_inbound(
        self,
        *,
        auth_token: str,
        public_url: str,
        twilio_number: str,
        human_number: str,
        text: str,
        valid: bool = True,
        original_replied_sid: str | None = None,
    ) -> SignedInbound:
        """A genuine inbound SMS webhook: the human's reply from ``human_number``
        to the deployment ``twilio_number``, carrying a valid
        ``X-Twilio-Signature`` = base64(HMAC-SHA1(auth_token, public_url +
        concat(sorted form pairs))). ``valid=False`` signs under the WRONG token
        (the fail-closed negative). ``public_url`` MUST equal the URL the door
        reconstructs from the request (scheme + Host + path). ``original_replied_sid``
        makes it a WhatsApp reply to that message: Twilio's ``OriginalRepliedMessageSid``
        and ``OriginalRepliedMessageSender`` (the deployment number) ride the form."""
        pairs = [
            ("To", twilio_number),
            ("From", human_number),
            ("Body", text),
            ("MessageSid", f"SM{uuid.uuid4().hex}"),
        ]
        if original_replied_sid is not None:
            pairs += [
                ("OriginalRepliedMessageSid", original_replied_sid),
                ("OriginalRepliedMessageSender", twilio_number),
            ]
        body = urlencode(pairs).encode("utf-8")
        key = auth_token if valid else auth_token + "-tampered"
        signed_payload = public_url + "".join(name + value for name, value in sorted(pairs))
        signature = base64.b64encode(
            hmac.new(key.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature,
        }
        return SignedInbound(headers=headers, body=body)

    def build_inbound_mms(
        self,
        *,
        auth_token: str,
        public_url: str,
        twilio_number: str,
        human_number: str,
        media_url: str,
        content_type: str,
        body: str = "",
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine inbound MMS webhook carrying ONE media item: ``NumMedia=1`` plus
        ``MediaUrl0``/``MediaContentType0``, with an optional ``Body`` caption. Signed like
        ``build_inbound`` (base64 HMAC-SHA1 over ``public_url`` + concat of sorted form pairs)."""
        pairs = [
            ("To", twilio_number),
            ("From", human_number),
            ("Body", body),
            ("NumMedia", "1"),
            ("MediaUrl0", media_url),
            ("MediaContentType0", content_type),
            ("MessageSid", f"MM{uuid.uuid4().hex}"),
        ]
        raw = urlencode(pairs).encode("utf-8")
        key = auth_token if valid else auth_token + "-tampered"
        signed_payload = public_url + "".join(name + value for name, value in sorted(pairs))
        signature = base64.b64encode(
            hmac.new(key.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        headers = {"content-type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
        return SignedInbound(headers=headers, body=raw)

    def build_status(
        self, *, auth_token: str, public_url: str, message_sid: str, status: str, valid: bool = True
    ) -> SignedInbound:
        """A genuine delivery-status webhook naming an outbound ``MessageSid`` — ``status``
        is Twilio's ``MessageStatus`` vocabulary (``delivered``/``failed``/``undelivered``).
        Signed like an inbound; ``valid=False`` signs under the WRONG token. ``public_url``
        MUST equal the URL the status door reconstructs from the request."""
        pairs = [("MessageSid", message_sid), ("MessageStatus", status)]
        body = urlencode(pairs).encode("utf-8")
        key = auth_token if valid else auth_token + "-tampered"
        signed_payload = public_url + "".join(name + value for name, value in sorted(pairs))
        signature = base64.b64encode(
            hmac.new(key.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        headers = {"content-type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
        return SignedInbound(headers=headers, body=body)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/Accounts/{account_sid}/Messages.json")
        async def create_message(account_sid: str, request: Request) -> JSONResponse:
            form = await request.form()
            sid = f"SM{uuid.uuid4().hex}"
            self.messages.append(
                {
                    "account_sid": account_sid,
                    "to": form.get("To"),
                    "from": form.get("From"),
                    "body": str(form.get("Body", "")),
                    # MMS media rides as repeated ``MediaUrl`` form fields (one per image
                    # item); ``getlist`` keeps every attachment, not just the last.
                    "media_urls": [str(url) for url in form.getlist("MediaUrl")],
                    "sid": sid,
                    # Wall-clock arrival, so a test can measure the gap between two sends.
                    "received_at": time.time(),
                }
            )
            if self.send_barrier is not None:
                await self.send_barrier()
            door = self.receipts.due(str(form.get("Body", "")))
            if door is None:
                return JSONResponse({"sid": sid, "status": "queued"}, status_code=201)
            status = self.build_status(auth_token=door.secret, public_url=door.url, message_sid=sid, status="delivered")
            receipt = BackgroundTask(self.receipts.post, door.url, status, sid)
            return JSONResponse({"sid": sid, "status": "queued"}, status_code=201, background=receipt)

        @app.get("/media/{ref:path}")
        async def media_redirect(ref: str) -> Response:
            # A ``MediaUrl`` GET (carrying HTTP Basic auth on this first hop) 307s to the CDN on the
            # other origin; the kit drops the Basic auth on that cross-origin hop.
            return Response(status_code=307, headers={"location": f"http://{self.host}:{self.cdn_port}/cdn/{ref}"})

        _install_catch_all(app, "twilio")
        return app

    def _build_cdn_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/cdn/{ref:path}")
        async def cdn_download(ref: str, request: Request) -> Response:
            # Record whether an ``Authorization`` rode to the CDN (it must NOT — the kit drops the
            # Basic auth on the cross-origin redirect) and serve the configured bytes.
            self.cdn_authorizations.append(request.headers.get("authorization"))
            blob = self.media.get(ref)
            if blob is None:
                return Response(status_code=404)
            return serve_blob(blob, self._media_served, ref)

        _install_catch_all(app, "twilio-cdn")
        return app
