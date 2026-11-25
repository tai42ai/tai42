"""A recording stub of the Meta WhatsApp (Graph) API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.channel_stubs.media import MediaBlob, serve_blob
from tai42_e2e.channel_stubs.receipts import AutoReceipts
from tai42_e2e.ports import allocate_port
from tai42_e2e.provider_stub import SignedInbound, _install_catch_all


@dataclass(frozen=True)
class FlowExchange:
    """One encrypted Flow data-endpoint request: the signed body to POST, and the key + IV its reply decrypts with."""

    inbound: SignedInbound
    aes_key: bytes
    iv: bytes


def _meta_timestamp(seconds: int | None) -> str:
    """A WhatsApp message's ``timestamp`` as Meta sends it: epoch seconds as a digit string."""
    return str(seconds if seconds is not None else int(time.time()))


def _set_reply_context(message: dict[str, Any], phone_number_id: str, context_id: str | None) -> None:
    """Make ``message`` a reply to ``context_id`` the way Meta relays one: a ``context`` naming the
    quoted wamid and the business number it came from. ``None`` leaves the message unquoting."""
    if context_id is not None:
        message["context"] = {"from": phone_number_id, "id": context_id}


class FakeWhatsApp:
    """A recording stub of the Meta WhatsApp (Graph) API.

    Serves ``POST /{phone_number_id}/messages`` (minting a ``wamid`` for EVERY message
    type — text, interactive buttons/list/flow, image, template — and recording the full
    JSON ``payload`` alongside the plain-text ``body``), recording the ``phone_number_id``
    the bridge sent FROM. It also serves the three Flow-lifecycle graph endpoints a form
    delivery hits when its Flow is uncached — ``GET /{waba_id}/flows`` (one page listing
    every recorded flow of that WABA, ``PUBLISHED`` once published, else ``DRAFT``),
    ``POST /{waba_id}/flows`` (minting a flow id, recorded in ``flows``) and
    ``POST /{flow_id}/publish`` (recorded in ``published_flows``) — and the Flow encryption
    key registration ``POST /{phone_number_id}/whatsapp_business_encryption`` (recorded in
    ``public_keys``, answered ``{"success": true}``). Any other path answers a loud 500.
    The Cloud API has no boot-time call (the webhook is configured in the Meta dashboard),
    so nothing is served for startup. The builders synthesize the requests aimed at the SUT's own
    ``/api/channels/whatsapp/inbound`` door: the GET verify handshake, a genuinely
    X-Hub-Signature-256-signed inbound text message, an interactive button/list reply, a
    completed-Flow ``nfm_reply``, a signed status webhook, and an encrypted Flow data-endpoint
    request (:meth:`build_flow_exchange`) whose reply :meth:`decrypt_flow_response` reads.
    Once ``receipts.door`` is set, each accepted ``/messages`` send is followed by a signed
    ``delivered`` status for its wamid (:class:`AutoReceipts`)."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.sent: list[dict[str, Any]] = []
        # The Flows created / published on the form-delivery path (Flow uncached):
        # ``flows`` records each create's ``{waba_id, name, flow_json, endpoint_uri, id}``,
        # ``published_flows`` the ids the publish step confirmed. The flow list serves both.
        self.flows: list[dict[str, Any]] = []
        self.published_flows: list[str] = []
        # The business public keys registered for a sending number:
        # ``{token, phone_number_id, business_public_key}`` per call.
        self.public_keys: list[dict[str, Any]] = []
        # Per-``media_id`` inbound-media config for the ingest suites. ``media_meta`` holds the
        # metadata GET response (``mime_type``/``sha256``/``file_size`` and an optional non-200
        # ``status`` for a permanent miss); ``media`` holds the Bearer-guarded lookaside byte blob.
        # ``_media_served`` counts lookaside fetches for ``MediaBlob.fail_times``.
        self.media_meta: dict[str, dict[str, Any]] = {}
        self.media: dict[str, MediaBlob] = {}
        self._media_served: dict[str, int] = {}
        self.receipts = AutoReceipts()
        self._wamids = itertools.count(1)
        self._flow_ids = itertools.count(1)
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def api_base_url(self) -> str:
        """The value ``CHANNEL_WHATSAPP_API_BASE_URL`` points at (the plugin
        addresses ``{api_base_url}/{phone_number_id}/messages``)."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def reset(self) -> None:
        self.sent.clear()
        self.flows.clear()
        self.published_flows.clear()
        self.public_keys.clear()
        self.media_meta.clear()
        self.media.clear()
        self._media_served.clear()
        self.receipts.reset()

    def sends_matching(self, text: str) -> list[dict[str, Any]]:
        return [record for record in self.sent if text in record["body"]]

    def payloads_matching(self, marker: str) -> list[dict[str, Any]]:
        """Recorded sends whose full JSON ``payload`` carries ``marker`` anywhere — the
        match for a send with no plain-text ``body`` (a template's named content, an
        image link/caption) that ``sends_matching`` (body-only) cannot see."""
        return [record for record in self.sent if marker in json.dumps(record["payload"])]

    def _mint_wamid(self) -> str:
        return f"wamid.{next(self._wamids):08d}"

    @staticmethod
    def _record_body(payload: dict[str, Any]) -> str:
        """The human-readable body text of a send whatever its type — a text body, an
        interactive message's body text, or an image caption — so a text-marker match
        (``sends_matching``) finds the send regardless of the native shape a select ask
        or a media send took. A template send carries no freeform body (its content is
        the named template); match those on ``payloads_matching`` instead."""
        kind = payload.get("type")
        if kind == "text":
            return str((payload.get("text") or {}).get("body", ""))
        if kind == "interactive":
            return str((payload.get("interactive") or {}).get("body", {}).get("text", ""))
        if kind == "image":
            return str((payload.get("image") or {}).get("caption") or "")
        return ""

    def verify_params(self, *, verify_token: str, challenge: str, mode: str = "subscribe") -> dict[str, str]:
        """The query params for Meta's GET subscription handshake against the SUT door —
        the door echoes ``hub.challenge`` iff ``hub.verify_token`` matches."""
        return {"hub.mode": mode, "hub.verify_token": verify_token, "hub.challenge": challenge}

    def build_inbound(
        self,
        *,
        app_secret: str,
        phone_number_id: str,
        wa_id: str,
        text: str,
        wamid: str | None = None,
        timestamp: int | None = None,
        valid: bool = True,
        context_id: str | None = None,
    ) -> SignedInbound:
        """A genuine uncorrelated inbound text message: the batched webhook value carries
        ``metadata.phone_number_id`` (the identity we are texted at) and one text message
        FROM ``wa_id``, signed X-Hub-Signature-256 over the RAW body under the app secret.
        ``valid=False`` signs under the WRONG secret (the fail-closed negative). A pinned
        ``wamid`` lets a replay POST the identical body. ``timestamp`` is the message's Meta
        ``timestamp`` in epoch seconds (now when omitted). ``context_id`` makes it a reply to
        that wamid (Meta's ``context.id``); omitted, it replies to nothing."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        message: dict[str, Any] = {
            "id": message_id,
            "from": wa_id,
            "timestamp": _meta_timestamp(timestamp),
            "type": "text",
            "text": {"body": text},
        }
        _set_reply_context(message, phone_number_id, context_id)
        value = {"metadata": {"phone_number_id": phone_number_id}, "messages": [message]}
        return self._sign_batch(value, app_secret=app_secret, valid=valid)

    def build_inbound_image(
        self,
        *,
        app_secret: str,
        phone_number_id: str,
        wa_id: str,
        media_id: str,
        mime_type: str = "image/jpeg",
        caption: str | None = None,
        sha256: str | None = None,
        wamid: str | None = None,
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine inbound IMAGE message: the media object carries the Graph ``id``
        (``media_id``), ``mime_type`` and an optional ``caption``/``sha256``. Signed
        X-Hub-Signature-256 like ``build_inbound``; a pinned ``wamid`` lets a replay POST the
        identical body (a vendor redelivery, deduped per ``wamid`` at the door)."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        image: dict[str, Any] = {"id": media_id, "mime_type": mime_type}
        if caption is not None:
            image["caption"] = caption
        if sha256 is not None:
            image["sha256"] = sha256
        value = {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [
                {
                    "id": message_id,
                    "from": wa_id,
                    "timestamp": _meta_timestamp(None),
                    "type": "image",
                    "image": image,
                }
            ],
        }
        return self._sign_batch(value, app_secret=app_secret, valid=valid)

    def build_inbound_error(
        self,
        *,
        app_secret: str,
        phone_number_id: str,
        wa_id: str,
        code: int,
        title: str,
        wamid: str | None = None,
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine inbound ERROR notice: a message carrying an ``errors`` array
        ``[{"code", "title"}]`` and no type. ``code == 131051`` ("Unsupported message type") names
        participant content the channel cannot receive → the generic refusal + event; other codes
        stay operator-log-only. Signed like ``build_inbound``; a pinned ``wamid`` replays it."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        value = {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [
                {
                    "id": message_id,
                    "from": wa_id,
                    "timestamp": _meta_timestamp(None),
                    "errors": [{"code": code, "title": title}],
                }
            ],
        }
        return self._sign_batch(value, app_secret=app_secret, valid=valid)

    def build_interactive_reply(
        self,
        *,
        app_secret: str,
        phone_number_id: str,
        wa_id: str,
        reply_kind: str,
        reply_id: str,
        title: str,
        wamid: str | None = None,
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine interactive-reply inbound: a tapped reply button
        (``reply_kind="button_reply"``) or a picked list row (``"list_reply"``). The
        reply's ``id`` is the question-bound id the outbound carried
        (``{interaction_id}:{index}``) and ``title`` the option's visible label — both
        echoed exactly as Meta relays a tap. Signed X-Hub-Signature-256 like
        ``build_inbound``; ``valid=False`` signs under the WRONG secret."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        value = {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [
                {
                    "id": message_id,
                    "from": wa_id,
                    "timestamp": _meta_timestamp(None),
                    "type": "interactive",
                    "interactive": {"type": reply_kind, reply_kind: {"id": reply_id, "title": title}},
                }
            ],
        }
        return self._sign_batch(value, app_secret=app_secret, valid=valid)

    def build_nfm_reply(
        self,
        *,
        app_secret: str,
        phone_number_id: str,
        wa_id: str,
        response: dict[str, Any],
        wamid: str | None = None,
        valid: bool = True,
        context_id: str | None = None,
        response_json: str | None = None,
    ) -> SignedInbound:
        """A genuine completed-Flow reply: Meta relays the filled form as an
        ``nfm_reply`` whose ``response_json`` is the form values (Flow number inputs
        arrive as strings) as a JSON STRING, carrying the ``flow_token`` the send set
        (the pending ask's ``interaction_id``). Signed X-Hub-Signature-256 like
        ``build_inbound``; ``valid=False`` signs under the WRONG secret. ``context_id``
        names the Flow message the reply answers (Meta's ``context.id``); omitted, the
        reply carries no context. ``response_json`` sets that string verbatim in place of
        the encoded ``response``."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        encoded = json.dumps(response) if response_json is None else response_json
        message: dict[str, Any] = {
            "id": message_id,
            "from": wa_id,
            "timestamp": _meta_timestamp(None),
            "type": "interactive",
            "interactive": {"type": "nfm_reply", "nfm_reply": {"response_json": encoded}},
        }
        _set_reply_context(message, phone_number_id, context_id)
        value = {"metadata": {"phone_number_id": phone_number_id}, "messages": [message]}
        return self._sign_batch(value, app_secret=app_secret, valid=valid)

    def build_status(
        self,
        *,
        app_secret: str,
        phone_number_id: str,
        wamid: str,
        status: str,
        recipient_id: str = "",
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine delivery-status webhook naming an outbound ``wamid`` — ``status`` is
        Meta's vocabulary (``sent``/``delivered``/``failed``/``read``). Signed like an
        inbound; ``valid=False`` signs under the WRONG secret."""
        value = {
            "metadata": {"phone_number_id": phone_number_id},
            "statuses": [{"id": wamid, "status": status, "recipient_id": recipient_id}],
        }
        return self._sign_batch(value, app_secret=app_secret, valid=valid)

    def build_flow_exchange(
        self, *, app_secret: str, private_key_pem: str, payload: dict[str, Any], valid: bool = True
    ) -> FlowExchange:
        """A Flow data-endpoint request encrypted exactly as Meta encrypts it, signed like a webhook.

        A fresh AES-128 key and 16-byte IV encrypt ``payload`` (AES-GCM); the key is wrapped with
        the business key's PUBLIC half (RSA-OAEP, SHA-256) into Meta's three-field envelope, and the
        raw body is signed X-Hub-Signature-256 (``valid=False`` signs under the WRONG secret). The
        returned key and IV decrypt the endpoint's reply (:meth:`decrypt_flow_response`).
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        assert isinstance(private_key, rsa.RSAPrivateKey), "the Flow business key must be an RSA key"
        aes_key = os.urandom(16)
        iv = os.urandom(16)
        wrapped = private_key.public_key().encrypt(
            aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        )
        envelope = {
            "encrypted_aes_key": base64.b64encode(wrapped).decode(),
            "initial_vector": base64.b64encode(iv).decode(),
            "encrypted_flow_data": base64.b64encode(
                AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), None)
            ).decode(),
        }
        body = json.dumps(envelope).encode()
        key = app_secret if valid else app_secret + "-tampered"
        digest = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers = {"content-type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"}
        return FlowExchange(inbound=SignedInbound(headers=headers, body=body), aes_key=aes_key, iv=iv)

    @staticmethod
    def decrypt_flow_response(body: bytes, exchange: FlowExchange) -> dict[str, Any]:
        """The data endpoint's reply decrypted as Meta decrypts it: the request's AES key, the bit-flipped IV."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        flipped = bytes(byte ^ 0xFF for byte in exchange.iv)
        return json.loads(AESGCM(exchange.aes_key).decrypt(flipped, base64.b64decode(body), None))

    def _sign_batch(self, value: dict[str, Any], *, app_secret: str, valid: bool) -> SignedInbound:
        body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}).encode()
        key = app_secret if valid else app_secret + "-tampered"
        digest = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers = {"content-type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"}
        return SignedInbound(headers=headers, body=body)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/{phone_number_id}/messages")
        async def create_message(phone_number_id: str, request: Request) -> JSONResponse:
            payload = await request.json()
            wamid = self._mint_wamid()
            body = self._record_body(payload)
            self.sent.append(
                {
                    "token": (request.headers.get("authorization", "")).removeprefix("Bearer "),
                    "phone_number_id": phone_number_id,
                    "to": payload.get("to"),
                    "type": payload.get("type"),
                    "body": body,
                    "payload": payload,
                    "wamid": wamid,
                    # Wall-clock arrival, so a test can measure the gap between two sends.
                    "received_at": time.time(),
                }
            )
            answer = {
                "messaging_product": "whatsapp",
                "contacts": [{"wa_id": payload.get("to")}],
                "messages": [{"id": wamid}],
            }
            # A mark-read (``status`` + ``message_id``, no recipient) is not a message, so it gets no receipt.
            door = self.receipts.due(body) if "to" in payload else None
            if door is None:
                return JSONResponse(answer)
            status = self.build_status(
                app_secret=door.secret,
                phone_number_id=phone_number_id,
                wamid=wamid,
                status="delivered",
                recipient_id=str(payload.get("to", "")),
            )
            return JSONResponse(answer, background=BackgroundTask(self.receipts.post, door.url, status, wamid))

        @app.get("/{waba_id}/flows")
        async def list_flows(waba_id: str) -> JSONResponse:
            # Meta's List Flows answer as one last page (no ``paging.next``).
            data = [
                {
                    "id": flow["id"],
                    "name": flow["name"],
                    "status": "PUBLISHED" if flow["id"] in self.published_flows else "DRAFT",
                    "categories": ["OTHER"],
                    "validation_errors": [],
                }
                for flow in self.flows
                if flow["waba_id"] == waba_id
            ]
            cursors = {"before": "cursor-first", "after": "cursor-last"} if data else {}
            return JSONResponse({"data": data, "paging": {"cursors": cursors}})

        @app.post("/{waba_id}/flows")
        async def create_flow(waba_id: str, request: Request) -> JSONResponse:
            payload = await request.json()
            flow_id = f"flow_{next(self._flow_ids):08d}"
            self.flows.append(
                {
                    "waba_id": waba_id,
                    "name": payload.get("name"),
                    "flow_json": payload.get("flow_json"),
                    "endpoint_uri": payload.get("endpoint_uri"),
                    "id": flow_id,
                }
            )
            return JSONResponse({"id": flow_id})

        @app.post("/{flow_id}/publish")
        async def publish_flow(flow_id: str, request: Request) -> JSONResponse:
            self.published_flows.append(flow_id)
            return JSONResponse({"success": True})

        @app.post("/{phone_number_id}/whatsapp_business_encryption")
        async def set_business_public_key(phone_number_id: str, request: Request) -> JSONResponse:
            payload = await request.json()
            self.public_keys.append(
                {
                    "token": (request.headers.get("authorization", "")).removeprefix("Bearer "),
                    "phone_number_id": phone_number_id,
                    "business_public_key": payload.get("business_public_key"),
                }
            )
            return JSONResponse({"success": True})

        @app.get("/lookaside/{media_id}")
        async def lookaside(media_id: str, request: Request) -> Response:
            # The short-lived lookaside URL the metadata GET handed back; the plugin sets the
            # Bearer on this GET directly (no redirect hop, so the kit carries the caller header),
            # and the bytes are served.
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return Response(status_code=401)
            blob = self.media.get(media_id)
            if blob is None:
                return Response(status_code=404)
            return serve_blob(blob, self._media_served, media_id)

        @app.get("/{media_id}")
        async def media_metadata(media_id: str, request: Request) -> Response:
            # Meta's Graph media-by-id metadata endpoint (Bearer): returns a short-lived lookaside
            # ``url`` on THIS origin plus the declared ``mime_type``/``sha256``/``file_size``. A
            # configured non-200 ``status`` (an unknown/expired media) is a permanent miss.
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return Response(status_code=401)
            meta = self.media_meta.get(media_id)
            if meta is None:
                return Response(status_code=404)
            status = int(meta.get("status", 200))
            if status != 200:
                return Response(status_code=status)
            result: dict[str, Any] = {"id": media_id, "url": f"{self.api_base_url}/lookaside/{media_id}"}
            for key in ("mime_type", "sha256", "file_size"):
                if meta.get(key) is not None:
                    result[key] = meta[key]
            return JSONResponse(result)

        _install_catch_all(app, "whatsapp")
        return app
