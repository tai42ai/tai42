"""A recording stub of the Telegram Bot API."""

from __future__ import annotations

import itertools
import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.channel_stubs.media import MediaBlob, serve_blob
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
    resolved to), ``answerCallbackQuery`` (recording the answered query id) and
    ``setWebhook`` (which the telegram plugin's ``on_startup`` hook calls, so boot
    succeeds against the stub — both replicas fire it, one record each). Any other
    path answers a loud 500."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.sent: list[dict[str, Any]] = []
        self.webhooks: list[dict[str, Any]] = []
        self.callback_answers: list[str] = []
        # Per-``file_id`` byte-serving behaviour for the inbound-media ingest suites, keyed by the
        # file_id ``getFile`` resolves; ``_media_served`` counts fetches per file_id so a
        # ``MediaBlob.fail_times`` can serve the transient fault before the healthy redelivery.
        self.media: dict[str, MediaBlob] = {}
        self._media_served: dict[str, int] = {}
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
        self.callback_answers.clear()
        self.media.clear()
        self._media_served.clear()

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

    def _inbound_update(
        self, *, secret: str, chat_id: str, message: dict[str, Any], update_id: int | None = None
    ) -> SignedInbound:
        """A signed inbound update wrapping ``message`` (its own ``update_id``/``message_id``
        minted unless ``update_id`` is pinned for a redelivery replay)."""
        uid = update_id if update_id is not None else next(self._ids)
        body = json.dumps(
            {"update_id": uid, "message": {"message_id": next(self._ids), "chat": {"id": int(chat_id)}, **message}}
        ).encode()
        headers = {"content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret}
        return SignedInbound(headers=headers, body=body)

    def build_inbound_text(self, *, secret: str, chat_id: str, text: str) -> SignedInbound:
        """A genuine plain text update from ``chat_id`` (an uncorrelated bridge message)."""
        return self._inbound_update(secret=secret, chat_id=chat_id, message={"text": text})

    def build_inbound_photo(
        self,
        *,
        secret: str,
        chat_id: str,
        file_id: str,
        caption: str | None = None,
        file_size: int | None = None,
        update_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> SignedInbound:
        """A genuine inbound PHOTO update: the largest size carries ``file_id``/``file_size``
        (Telegram sends sizes ascending, so the door reads the last), with an optional ``caption``.
        ``update_id`` may be pinned to replay the identical update (a vendor redelivery, deduped per
        ``update_id`` at the accept seam). ``reply_to_message_id`` sends the photo as a reply to
        that message (``message.reply_to_message.message_id``); omitted, it replies to nothing."""
        largest: dict[str, Any] = {"file_id": file_id, "width": 1280, "height": 960}
        if file_size is not None:
            largest["file_size"] = file_size
        message: dict[str, Any] = {"from": {"id": int(chat_id)}, "photo": [{"file_id": f"{file_id}-thumb"}, largest]}
        if caption is not None:
            message["caption"] = caption
        if reply_to_message_id is not None:
            message["reply_to_message"] = {"message_id": reply_to_message_id}
        return self._inbound_update(secret=secret, chat_id=chat_id, message=message, update_id=update_id)

    def build_inbound_document(
        self,
        *,
        secret: str,
        chat_id: str,
        file_id: str,
        mime_type: str,
        file_name: str,
        caption: str | None = None,
        file_size: int | None = None,
        update_id: int | None = None,
    ) -> SignedInbound:
        """A genuine inbound DOCUMENT update: the ``document`` member carries ``file_id``, the
        vendor-declared ``mime_type``/``file_name`` and an optional ``file_size``, with an optional
        ``caption``. A document ingests to the served-attachment ``DOCUMENT`` kind."""
        document: dict[str, Any] = {"file_id": file_id, "mime_type": mime_type, "file_name": file_name}
        if file_size is not None:
            document["file_size"] = file_size
        message: dict[str, Any] = {"from": {"id": int(chat_id)}, "document": document}
        if caption is not None:
            message["caption"] = caption
        return self._inbound_update(secret=secret, chat_id=chat_id, message=message, update_id=update_id)

    def build_callback_query(
        self,
        *,
        secret: str,
        chat_id: str,
        message_id: int,
        data: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> SignedInbound:
        """A genuine inline-keyboard tap: a ``callback_query`` update whose ``message`` is the
        message holding the keyboard (``message_id`` in chat ``chat_id``) and whose ``data`` is the
        tapped button's ``callback_data``. ``keyboard`` is that message's ``inline_keyboard`` rows
        (buttons of ``text`` + ``callback_data``) as Telegram echoes them; omitted, the message
        carries no ``reply_markup``."""
        message: dict[str, Any] = {"message_id": message_id, "chat": {"id": int(chat_id)}}
        if keyboard is not None:
            message["reply_markup"] = {"inline_keyboard": keyboard}
        body = json.dumps(
            {
                "update_id": next(self._ids),
                "callback_query": {
                    "id": f"cbq-{next(self._ids)}",
                    "from": {"id": int(chat_id)},
                    "data": data,
                    "message": message,
                },
            }
        ).encode()
        headers = {"content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret}
        return SignedInbound(headers=headers, body=body)

    def build_inbound_poll(self, *, secret: str, chat_id: str, update_id: int | None = None) -> SignedInbound:
        """A genuine inbound POLL update — content Telegram recognises that the channel cannot
        map to a turn, so the door replies with the generic refusal + event. ``update_id`` may be
        pinned to replay the identical update (a vendor redelivery)."""
        message = {"from": {"id": int(chat_id)}, "poll": {"id": "poll-1", "question": "?", "options": []}}
        return self._inbound_update(secret=secret, chat_id=chat_id, message=message, update_id=update_id)

    def build_inbound_member(self, *, secret: str, chat_id: str, member: str, value: Any) -> SignedInbound:
        """A genuine inbound update whose message carries ``member`` set to ``value`` and no text."""
        message = {"from": {"id": int(chat_id)}, member: value}
        return self._inbound_update(secret=secret, chat_id=chat_id, message=message)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/bot{token}/sendChatAction")
        async def send_chat_action(token: str, request: Request) -> JSONResponse:
            # The plugin fires a "typing" action per inbound; record nothing, ack like the real API.
            await request.json()
            return JSONResponse({"ok": True, "result": True})

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
                    "result_chat_id": chat_id,
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

        @app.post("/bot{token}/answerCallbackQuery")
        async def answer_callback_query(token: str, request: Request) -> JSONResponse:
            payload = await request.json()
            self.callback_answers.append(str(payload.get("callback_query_id")))
            return JSONResponse({"ok": True, "result": True})

        @app.post("/bot{token}/setWebhook")
        async def set_webhook(token: str, request: Request) -> JSONResponse:
            payload = await request.json()
            self.webhooks.append({"token": token, **payload})
            return JSONResponse({"ok": True, "result": True})

        @app.post("/bot{token}/getFile")
        async def get_file(token: str, request: Request) -> JSONResponse:
            # Resolve a ``file_id`` to a temporary ``file_path``; the plugin then streams the bytes
            # from ``/file/bot{token}/{file_path}``. The stub keys the path on the file_id itself so
            # the byte handler can find the configured blob.
            payload = await request.json()
            file_id = str(payload.get("file_id"))
            return JSONResponse({"ok": True, "result": {"file_id": file_id, "file_path": file_id}})

        @app.get("/file/bot{token}/{file_path:path}")
        async def download_file(token: str, file_path: str) -> Response:
            # The file_path IS the file_id (see ``getFile``); serve the configured bytes for it.
            blob = self.media.get(file_path)
            if blob is None:
                return Response(status_code=404)
            return serve_blob(blob, self._media_served, file_path)

        _install_catch_all(app, "telegram")
        return app
