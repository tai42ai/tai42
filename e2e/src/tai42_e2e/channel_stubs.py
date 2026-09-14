"""Recording in-process stubs of the four channel providers' APIs, each a thread-
hosted FastAPI on an allocated loopback port. Each records the plugin's outbound
calls, mints the id the real provider would, and carries a helper that builds a
genuinely signed inbound request so the plugin's REAL signature verification runs
against it — never a bypass. Any unexpected path is a loud 500."""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port
from tai42_e2e.provider_stub import SignedInbound, _install_catch_all


def _telegram_chat_id(raw: Any) -> int:
    """The numeric ``chat.id`` a real Bot API send resolves to.

    Telegram's returned Message objects always carry a numeric ``chat.id`` regardless
    of whether the send addressed a numeric chat id or an ``@username``. A numeric
    recipient echoes back verbatim as an int; a non-numeric ``@username`` resolves to a
    deterministic synthetic numeric id (as the real API would), so the stub never leaks
    a string where the plugin requires an int."""
    text = str(raw)
    if text.lstrip("-").isdigit():
        return int(text)
    return abs(hash(text)) % 1_000_000_000 + 1


class FakeTelegram:
    """A recording stub of the Telegram Bot API.

    Serves ``sendMessage`` and ``sendPhoto`` (each minting a monotonically increasing
    ``message_id`` and returning a Message with the numeric ``chat.id`` the send
    resolved to) and ``setWebhook`` (which the telegram plugin's ``on_startup`` hook
    calls, so boot succeeds against the stub — both replicas fire it, one record each).
    Any other path answers a loud 500."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.sent: list[dict[str, Any]] = []
        self.webhooks: list[dict[str, Any]] = []
        self._ids = itertools.count(1000)
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def api_base_url(self) -> str:
        """The value ``CHANNEL_TELEGRAM_API_BASE_URL`` points at (the Bot API
        origin the plugin composes ``/bot{token}/{method}`` under)."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def reset(self) -> None:
        self.sent.clear()
        self.webhooks.clear()

    def sends_matching(self, text: str) -> list[dict[str, Any]]:
        """Recorded ``sendMessage`` payloads whose text carries ``text`` — the
        per-spec ``uniq`` filter that keeps an assertion off shared state."""
        return [record for record in self.sent if text in record["text"]]

    def build_inbound(self, *, secret: str, chat_id: str, reply_to_message_id: int, text: str) -> SignedInbound:
        """A genuine ForceReply update: the secret rides
        ``X-Telegram-Bot-Api-Secret-Token`` (what the door compares constant-time)
        and ``message.reply_to_message.message_id`` carries the delivered id."""
        body = json.dumps(
            {
                "update_id": next(self._ids),
                "message": {
                    "message_id": next(self._ids),
                    "chat": {"id": int(chat_id)},
                    "text": text,
                    "reply_to_message": {"message_id": reply_to_message_id},
                },
            }
        ).encode()
        headers = {"content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret}
        return SignedInbound(headers=headers, body=body)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/bot{token}/sendMessage")
        async def send_message(token: str, request: Request) -> JSONResponse:
            payload = await request.json()
            message_id = next(self._ids)
            chat_id = _telegram_chat_id(payload.get("chat_id"))
            self.sent.append(
                {
                    "token": token,
                    "chat_id": str(payload.get("chat_id")),
                    "text": payload.get("text", ""),
                    "reply_markup": payload.get("reply_markup"),
                    "message_id": message_id,
                    "method": "sendMessage",
                }
            )
            # A real Message object always carries a ``chat`` with a numeric ``id`` — the
            # authoritative chat Telegram resolved the send to (the writer scopes its
            # correlation anchor by it). Return the numeric id, never the raw chat_id.
            return JSONResponse({"ok": True, "result": {"message_id": message_id, "chat": {"id": chat_id}}})

        @app.post("/bot{token}/sendPhoto")
        async def send_photo(token: str, request: Request) -> JSONResponse:
            payload = await request.json()
            message_id = next(self._ids)
            chat_id = _telegram_chat_id(payload.get("chat_id"))
            self.sent.append(
                {
                    "token": token,
                    "chat_id": str(payload.get("chat_id")),
                    "text": payload.get("caption", ""),
                    "photo": payload.get("photo"),
                    "reply_markup": payload.get("reply_markup"),
                    "message_id": message_id,
                    "method": "sendPhoto",
                }
            )
            # sendPhoto returns the same Message shape as sendMessage (with a ``photo``);
            # the writer reads only ``message_id`` / ``chat.id`` from it.
            return JSONResponse({"ok": True, "result": {"message_id": message_id, "chat": {"id": chat_id}}})

        @app.post("/bot{token}/setWebhook")
        async def set_webhook(token: str, request: Request) -> JSONResponse:
            payload = await request.json()
            self.webhooks.append({"token": token, **payload})
            return JSONResponse({"ok": True, "result": True})

        _install_catch_all(app, "telegram")
        return app


class FakeSlack:
    """A recording stub of the Slack Web + Events API.

    Serves ``chat.postMessage`` (minting a ``ts`` thread anchor, recording the
    Block Kit ``blocks`` alongside the text so a form message's button is
    readable) and ``views.open`` (recording the modal ``view`` the interactivity
    door opens for a form question). Any other path answers a loud 500. Slack has
    no boot-time API call (its Events API Request URL is configured in the
    dashboard, not per process start), so nothing is served for startup."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.posts: list[dict[str, Any]] = []
        # The modal views ``views.open`` opened — the form leg reads the view a
        # ``block_actions`` click drove the interactivity door to open.
        self.views: list[dict[str, Any]] = []
        self._ts = itertools.count(1)
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def api_base_url(self) -> str:
        """The value ``CHANNEL_SLACK_API_BASE_URL`` points at (the plugin
        addresses ``{api_base_url}/chat.postMessage``)."""
        return f"http://{self.host}:{self.port}/api"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def reset(self) -> None:
        self.posts.clear()
        self.views.clear()

    def sends_matching(self, text: str) -> list[dict[str, Any]]:
        return [record for record in self.posts if text in record["text"]]

    def build_interactive(self, *, signing_secret: str, payload: dict[str, Any], valid: bool = True) -> SignedInbound:
        """A genuine Block Kit interactivity POST for the interactivity door: the JSON
        ``payload`` ride the single ``payload`` form field (``application/x-www-form-
        urlencoded``), carrying a valid Slack v0 HMAC over ``v0:{ts}:{body}`` computed
        with the signing secret. ``valid=False`` signs under the WRONG secret. The
        caller builds the ``block_actions`` / ``view_submission`` payload shape."""
        timestamp = str(int(time.time()))
        body = urlencode({"payload": json.dumps(payload)}).encode()
        key = signing_secret if valid else signing_secret + "-tampered"
        base = b"v0:" + timestamp.encode("ascii") + b":" + body
        digest = hmac.new(key.encode("utf-8"), base, hashlib.sha256).hexdigest()
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={digest}",
        }
        return SignedInbound(headers=headers, body=body)

    def build_inbound(
        self, *, signing_secret: str, channel: str, thread_ts: str, text: str, event_id: str, valid: bool = True
    ) -> SignedInbound:
        """A genuine Events API ``event_callback``: a thread reply whose
        ``thread_ts`` is the delivered ``ts``, carrying a valid Slack v0 HMAC over
        ``v0:{ts}:{body}`` computed with the signing secret. ``valid=False`` mints
        the same envelope under the WRONG secret (the fail-closed negative)."""
        timestamp = str(int(time.time()))
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": event_id,
                "event": {
                    "type": "message",
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "ts": f"{next(self._ts)}.000100",
                    "text": text,
                },
            }
        ).encode()
        key = signing_secret if valid else signing_secret + "-tampered"
        base = b"v0:" + timestamp.encode("ascii") + b":" + body
        digest = hmac.new(key.encode("utf-8"), base, hashlib.sha256).hexdigest()
        headers = {
            "content-type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={digest}",
        }
        return SignedInbound(headers=headers, body=body)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/api/chat.postMessage")
        async def post_message(request: Request) -> JSONResponse:
            payload = await request.json()
            ts = f"{next(self._ts)}.000100"
            self.posts.append(
                {
                    "token": (request.headers.get("authorization", "")).removeprefix("Bearer "),
                    "channel": payload.get("channel"),
                    "text": payload.get("text", ""),
                    "blocks": payload.get("blocks"),
                    "ts": ts,
                }
            )
            return JSONResponse({"ok": True, "ts": ts})

        @app.post("/api/views.open")
        async def views_open(request: Request) -> JSONResponse:
            payload = await request.json()
            self.views.append(payload.get("view"))
            return JSONResponse({"ok": True})

        _install_catch_all(app, "slack")
        return app


class FakeTwilio:
    """A recording stub of the Twilio REST Messages API.

    Serves ``/Accounts/{sid}/Messages.json`` (minting a ``MessageSid``). Any other
    path answers a loud 500. Twilio has no boot-time API call (the number's
    inbound webhook is configured out-of-band), so nothing is served for
    startup."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.messages: list[dict[str, Any]] = []
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def api_base_url(self) -> str:
        """The value ``CHANNEL_TWILIO_API_BASE_URL`` points at (the plugin
        addresses ``{api_base_url}/Accounts/{AccountSid}/Messages.json``)."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def reset(self) -> None:
        self.messages.clear()

    def sends_matching(self, text: str) -> list[dict[str, Any]]:
        return [record for record in self.messages if text in record["body"]]

    def build_inbound(
        self, *, auth_token: str, public_url: str, twilio_number: str, human_number: str, text: str, valid: bool = True
    ) -> SignedInbound:
        """A genuine inbound SMS webhook: the human's reply from ``human_number``
        to the deployment ``twilio_number``, carrying a valid
        ``X-Twilio-Signature`` = base64(HMAC-SHA1(auth_token, public_url +
        concat(sorted form pairs))). ``valid=False`` signs under the WRONG token
        (the fail-closed negative). ``public_url`` MUST equal the URL the door
        reconstructs from the request (scheme + Host + path)."""
        pairs = [
            ("To", twilio_number),
            ("From", human_number),
            ("Body", text),
            ("MessageSid", f"SM{uuid.uuid4().hex}"),
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
                }
            )
            return JSONResponse({"sid": sid, "status": "queued"}, status_code=201)

        _install_catch_all(app, "twilio")
        return app


class FakeWhatsApp:
    """A recording stub of the Meta WhatsApp (Graph) API.

    Serves ``POST /{phone_number_id}/messages`` (minting a ``wamid`` for EVERY message
    type — text, interactive buttons/list/flow, image, template — and recording the full
    JSON ``payload`` alongside the plain-text ``body``), recording the ``phone_number_id``
    the bridge sent FROM. It also serves the two Flow-lifecycle graph endpoints a form
    delivery hits when the schema is uncached — ``POST /{waba_id}/flows`` (minting a flow
    id, recorded in ``flows``) and ``POST /{flow_id}/publish`` (recorded in
    ``published_flows``). Any other path answers a loud 500. The Cloud API has no boot-time
    call (the webhook is configured in the Meta dashboard), so nothing is served for
    startup. The builders synthesize the requests aimed at the SUT's own
    ``/api/channels/whatsapp/inbound`` door: the GET verify handshake, a genuinely
    X-Hub-Signature-256-signed inbound text message, an interactive button/list reply, a
    completed-Flow ``nfm_reply``, and a signed status webhook."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.sent: list[dict[str, Any]] = []
        # The Flows created / published on the form-delivery path (schema uncached):
        # ``flows`` records each create's ``{waba_id, name, flow_json, id}``,
        # ``published_flows`` the ids the publish step confirmed.
        self.flows: list[dict[str, Any]] = []
        self.published_flows: list[str] = []
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
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine uncorrelated inbound text message: the batched webhook value carries
        ``metadata.phone_number_id`` (the identity we are texted at) and one text message
        FROM ``wa_id``, signed X-Hub-Signature-256 over the RAW body under the app secret.
        ``valid=False`` signs under the WRONG secret (the fail-closed negative). A pinned
        ``wamid`` lets a replay POST the identical body."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        value = {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [{"id": message_id, "from": wa_id, "type": "text", "text": {"body": text}}],
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
    ) -> SignedInbound:
        """A genuine completed-Flow reply: Meta relays the filled form as an
        ``nfm_reply`` whose ``response_json`` is the form values (Flow number inputs
        arrive as strings) as a JSON STRING, carrying the ``flow_token`` the send set
        (the pending ask's ``interaction_id``). Signed X-Hub-Signature-256 like
        ``build_inbound``; ``valid=False`` signs under the WRONG secret."""
        message_id = wamid if wamid is not None else self._mint_wamid()
        value = {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [
                {
                    "id": message_id,
                    "from": wa_id,
                    "type": "interactive",
                    "interactive": {"type": "nfm_reply", "nfm_reply": {"response_json": json.dumps(response)}},
                }
            ],
        }
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
            self.sent.append(
                {
                    "token": (request.headers.get("authorization", "")).removeprefix("Bearer "),
                    "phone_number_id": phone_number_id,
                    "to": payload.get("to"),
                    "type": payload.get("type"),
                    "body": self._record_body(payload),
                    "payload": payload,
                    "wamid": wamid,
                }
            )
            return JSONResponse(
                {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": payload.get("to")}],
                    "messages": [{"id": wamid}],
                }
            )

        @app.post("/{waba_id}/flows")
        async def create_flow(waba_id: str, request: Request) -> JSONResponse:
            payload = await request.json()
            flow_id = f"flow_{next(self._flow_ids):08d}"
            self.flows.append(
                {
                    "waba_id": waba_id,
                    "name": payload.get("name"),
                    "flow_json": payload.get("flow_json"),
                    "id": flow_id,
                }
            )
            return JSONResponse({"id": flow_id})

        @app.post("/{flow_id}/publish")
        async def publish_flow(flow_id: str, request: Request) -> JSONResponse:
            self.published_flows.append(flow_id)
            return JSONResponse({"success": True})

        _install_catch_all(app, "whatsapp")
        return app
