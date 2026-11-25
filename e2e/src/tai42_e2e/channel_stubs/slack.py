"""A recording stub of the Slack Web + Events API."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.channel_stubs.media import MediaBlob, serve_blob
from tai42_e2e.ports import allocate_port
from tai42_e2e.provider_stub import SignedInbound, _install_catch_all

# The default Slack sender id a synthesized event carries — a real Slack message/file_share event
# always names the participant in the top-level ``user`` (the channel reads it for the media
# origin). Distinct from the bot's own user id so a synthesized event is never taken as a self-echo.
SLACK_PARTICIPANT_USER = "U0PARTICIPANT"


class FakeSlack:
    """A recording stub of the Slack Web + Events API.

    Serves ``chat.postMessage`` (minting a ``ts`` thread anchor, recording the
    Block Kit ``blocks`` alongside the text so a form message's button is
    readable), ``views.open`` (recording the modal ``view`` the interactivity
    door opens for a form question) and ``views.update`` (recording each re-render
    of an open modal). Any other path answers a loud 500. Slack has
    no boot-time API call (its Events API Request URL is configured in the
    dashboard, not per process start), so nothing is served for startup."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.posts: list[dict[str, Any]] = []
        # The modal views ``views.open`` opened — the form leg reads the view a
        # ``block_actions`` click drove the interactivity door to open.
        self.views: list[dict[str, Any]] = []
        # The ``views.update`` re-renders, in order: each ``{"view_id", "hash", "view"}`` the
        # interactivity door sent when a reacting or conditional field changed.
        self.view_updates: list[dict[str, Any]] = []
        # Per-``url_private``-path byte-serving behaviour for the inbound-media ingest suites, keyed
        # by the path a test builds through :meth:`download_url`; the download requires the bot
        # Bearer (a missing/wrong token answers a 302, which the plugin — redirects off — treats as
        # not-the-bytes). ``_media_served`` counts fetches for ``MediaBlob.fail_times``.
        self.media: dict[str, MediaBlob] = {}
        self._media_served: dict[str, int] = {}
        self._ts = itertools.count(1)
        self._server = ThreadedServer(self._build_app(), host, self.port)

    def download_url(self, ref: str) -> str:
        """A ``url_private`` on this stub for media reference ``ref`` (the Bearer-guarded byte
        endpoint the slack channel fetches with ``Authorization: Bearer <bot_token>``)."""
        return f"http://{self.host}:{self.port}/files/{ref}"

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
        self.view_updates.clear()
        self.media.clear()
        self._media_served.clear()

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
        self,
        *,
        signing_secret: str,
        channel: str,
        thread_ts: str,
        text: str,
        event_id: str,
        user: str = SLACK_PARTICIPANT_USER,
        valid: bool = True,
        attachments: list[dict[str, Any]] | None = None,
    ) -> SignedInbound:
        """A genuine Events API ``event_callback``: a thread reply whose
        ``thread_ts`` is the delivered ``ts``, carrying the sender ``user`` and a valid Slack v0
        HMAC over ``v0:{ts}:{body}`` computed with the signing secret. ``valid=False`` mints
        the same envelope under the WRONG secret (the fail-closed negative). ``attachments``
        rides on the event as a shared message's attachments."""
        timestamp = str(int(time.time()))
        event: dict[str, Any] = {
            "type": "message",
            "channel": channel,
            "user": user,
            "thread_ts": thread_ts,
            "ts": f"{next(self._ts)}.000100",
            "text": text,
        }
        if attachments is not None:
            event["attachments"] = attachments
        body = json.dumps({"type": "event_callback", "event_id": event_id, "event": event}).encode()
        key = signing_secret if valid else signing_secret + "-tampered"
        base = b"v0:" + timestamp.encode("ascii") + b":" + body
        digest = hmac.new(key.encode("utf-8"), base, hashlib.sha256).hexdigest()
        headers = {
            "content-type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={digest}",
        }
        return SignedInbound(headers=headers, body=body)

    def build_inbound_files(
        self,
        *,
        signing_secret: str,
        channel: str,
        files: list[dict[str, Any]],
        event_id: str,
        text: str | None = None,
        user: str = SLACK_PARTICIPANT_USER,
        valid: bool = True,
    ) -> SignedInbound:
        """A genuine ``file_share`` Events API delivery: a message subtype carrying the sender
        ``user`` and ``files`` (each a Slack file object — ``url_private``/``name``/``mimetype``/
        ``size``), signed X-Hub like ``build_inbound``. A file exposing neither ``url_private`` nor
        ``name`` is the channel's unsupported case; every other file bridges one turn."""
        event: dict[str, Any] = {
            "type": "message",
            "subtype": "file_share",
            "channel": channel,
            "user": user,
            "ts": f"{next(self._ts)}.000200",
            "files": files,
        }
        if text is not None:
            event["text"] = text
        value = {"type": "event_callback", "event_id": event_id, "event": event}
        body = json.dumps(value).encode()
        timestamp = str(int(time.time()))
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
            # A real ok response names the conversation Slack posted into (``channel``) beside the
            # message ``ts``; the stub posts into the channel it was given.
            return JSONResponse({"ok": True, "channel": payload.get("channel"), "ts": ts})

        @app.post("/api/views.open")
        async def views_open(request: Request) -> JSONResponse:
            payload = await request.json()
            self.views.append(payload.get("view"))
            return JSONResponse({"ok": True})

        @app.post("/api/views.update")
        async def views_update(request: Request) -> JSONResponse:
            payload = await request.json()
            self.view_updates.append(
                {"view_id": payload.get("view_id"), "hash": payload.get("hash"), "view": payload.get("view")}
            )
            return JSONResponse({"ok": True})

        @app.get("/files/{ref:path}")
        async def download_file(ref: str, request: Request) -> Response:
            # ``url_private`` needs the bot Bearer; a missing/wrong token 302s (a 3xx the plugin,
            # redirects off, treats as not-the-bytes → permanent rejection), never leaking bytes.
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return Response(status_code=302, headers={"location": "https://slack.com/signin"})
            blob = self.media.get(ref)
            if blob is None:
                return Response(status_code=404)
            return serve_blob(blob, self._media_served, ref)

        _install_catch_all(app, "slack")
        return app
