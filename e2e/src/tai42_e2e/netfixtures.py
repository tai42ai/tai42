"""Harness network fixtures, all thread-hosted in the pytest process: an
observation target server, a recording CONNECT proxy, and a signing OIDC issuer.
None are compose services or SUT processes."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import itertools
import json
import select
import socket
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port
from tai42_e2e.waiting import wait_for

# ---- target server ------------------------------------------------------


@dataclass
class RequestRecord:
    path: str
    headers: dict[str, str]


class TargetServer:
    """An HTTP server the ``e2e_http_probe`` tool GETs. Records every request so
    a test can assert the target was actually hit (and only once)."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.records: list[RequestRecord] = []
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/ok")
        async def ok(request: Request) -> PlainTextResponse:
            self.records.append(RequestRecord(path="/ok", headers=dict(request.headers)))
            return PlainTextResponse("target-ok")

        @app.get("/doc.txt")
        async def doc(request: Request, body: str = "target-document") -> PlainTextResponse:
            # A text document the file_loader tool fetches and extracts. The ``.txt``
            # suffix gives the loader a type hint, and ``body`` lets a caller inject a
            # unique marker so the extracted text is asserted against what was served.
            self.records.append(RequestRecord(path="/doc.txt", headers=dict(request.headers)))
            return PlainTextResponse(body)

        @app.get("/slow")
        async def slow(request: Request, ms: int = 0) -> PlainTextResponse:
            self.records.append(RequestRecord(path="/slow", headers=dict(request.headers)))
            # A bounded server-side delay for the interleaving test; not a client
            # wait, so it is not the banned kind of sleep.
            time.sleep(ms / 1000.0)  # noqa: TID251 — server-side response latency, not a client poll
            return PlainTextResponse("target-slow")

        return app


# ---- recording CONNECT proxy -------------------------------------------


class _ConnectHandler(socketserver.BaseRequestHandler):
    """Handle one HTTP CONNECT tunnel: parse the target, record it, dial the
    target, answer 200, then relay bytes both ways until either side closes."""

    def handle(self) -> None:
        client = self.request
        header = self._read_headers(client)
        if not header:
            return
        request_line = header.decode("latin-1").split("\r\n", 1)[0]
        parts = request_line.split(" ")
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        target = parts[1]
        host, _, port_str = target.partition(":")
        port = int(port_str or "80")
        self.server.records.append(target)  # type: ignore[attr-defined]
        try:
            upstream = socket.create_connection((host, port), timeout=10.0)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self._relay(client, upstream)

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        socks = [a, b]
        try:
            while True:
                readable, _, errored = select.select(socks, [], socks, 30.0)
                if errored or not readable:
                    break
                for src in readable:
                    dst = b if src is a else a
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
        finally:
            for sock in socks:
                with contextlib.suppress(OSError):
                    sock.close()


class _ThreadingProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int]) -> None:
        super().__init__(addr, _ConnectHandler)
        self.records: list[str] = []


class RecordingConnectProxy:
    """A minimal HTTP CONNECT proxy that records each CONNECT target. "Went
    through the proxy" == an entry in :attr:`records`."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self._server = _ThreadingProxyServer((host, self.port))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def records(self) -> list[str]:
        return list(self._server.records)

    def start(self) -> None:
        self._thread.start()
        wait_for(
            lambda: self._server.socket.fileno() != -1,
            deadline=5.0,
            message="CONNECT proxy never bound",
        )

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


# ---- signing OIDC issuer -------------------------------------------------

# The fixed key id the issuer publishes in its JWKS and stamps in every token
# header; the verifier resolves the signing key by this kid.
_IDP_KID = "e2e-idp-key-1"
# The lifetime stamped onto a freshly minted id_token / machine JWT (``exp`` =
# ``iat`` + this). A negative clock offset (``set_clock_offset``) or ``expired=True``
# on ``mint_jwt`` moves ``exp`` into the past instead.
_ID_TOKEN_TTL_SECONDS = 3600


@dataclass
class _OAuthState:
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_count: int = 0
    expires_in: int = 3600
    issued_access_tokens: list[str] = field(default_factory=list)
    # Per-authorization-code record (the OIDC ``nonce`` the /authorize request
    # carried), so /token can echo it into the id_token — the id_token replay
    # binding the OIDC login callback verifies. Popped single-use at exchange.
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When set, ``/authorize`` redirects back with an OAuth error instead of a
    # code — the scripted user-denies-consent path the browser-e2e negative drives.
    deny: bool = False


class OAuthIdp:
    """A deterministic in-memory OIDC issuer + OAuth2 authorization server.

    OAuth2 surface (the connector suite): ``/authorize`` redirects back with a
    code, ``/token`` grants and refreshes access tokens, ``/userinfo`` returns a
    fixed identity, and refresh grants are counted for the refresh-lock test.

    OIDC surface (the accounts/identity OIDC suites): a per-instance RSA keypair
    signs RS256 tokens, ``/.well-known/openid-configuration`` advertises discovery,
    ``/jwks`` publishes the public key under a fixed ``kid``, and every ``/token``
    response carries a signed ``id_token`` (issuer = ``base_url``, ``aud`` = the
    construction ``client_id``, ``sub``/``email`` construction-fixed, ``iat``/``exp``
    from a controllable clock offset, ``nonce`` echoed from the authorize request).
    :meth:`mint_jwt` produces good AND deliberately-broken machine JWTs directly,
    without an HTTP round trip, for the assert-the-negative specs."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        *,
        client_id: str = "e2e-client",
        subject: str = "e2e-user",
        email: str = "e2e@example.com",
    ) -> None:
        self.host = host
        # A caller may pin the port (the browser-e2e runner does, so a Playwright
        # spec can flip the denial knob over HTTP at a known origin); otherwise
        # take an ephemeral one.
        self.port = port if port is not None else allocate_port()
        self.client_id = client_id
        self.subject = subject
        self.email = email
        # The default audience of tokens this issuer mints is the client it was
        # constructed for; a spec targeting the identity-oidc provider (whose
        # audience is configured independently) overrides it via ``mint_jwt(aud=…)``.
        self._audience = client_id
        # The signing keypair, per instance. ``_bad_key`` carries the SAME kid, so a token
        # it signs resolves the published key by kid and then fails the signature check (a
        # signature failure, not an unknown-kid failure).
        self._key = RSAKey.generate_key(2048, {"kid": _IDP_KID, "use": "sig", "alg": "RS256"})
        self._bad_key = RSAKey.generate_key(2048, {"kid": _IDP_KID, "use": "sig", "alg": "RS256"})
        # Seconds added to ``iat``/``exp`` on every minted token; a large negative
        # value makes tokens already-expired at the verifier without racing a real clock.
        self._clock_offset_seconds = 0
        # When true, the ``/token`` id_token is signed with the off-JWKS key (same
        # kid, wrong key) so the login callback resolves a key and then fails the
        # signature check. Default false leaves the normal good-signature flow intact.
        self._sign_id_token_badly = False
        self._state = _OAuthState()
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def refresh_count(self) -> int:
        return self._state.refresh_count

    @property
    def issued_access_tokens(self) -> list[str]:
        """Every access token this IdP has minted — the encryption-at-rest test
        asserts none of them appears verbatim in the stored connection blob."""
        return list(self._state.issued_access_tokens)

    def set_expires_in(self, seconds: int) -> None:
        """Set the lifetime of freshly issued access tokens (a value ``<= 0``
        makes the next-issued token already expired, forcing a refresh)."""
        self._state.expires_in = seconds

    def set_clock_offset(self, seconds: int) -> None:
        """Shift the ``iat``/``exp`` stamped on every minted id_token / machine JWT
        by ``seconds`` (a large negative value mints already-expired tokens through
        the /token flow, the login-flow analogue of ``mint_jwt(expired=True)``)."""
        self._clock_offset_seconds = seconds

    def set_deny(self, deny: bool) -> None:
        """Script the authorization decision: when ``deny`` is true the next
        ``/authorize`` redirects back with ``error=access_denied`` (no code)
        instead of granting one — the user-refused-consent negative."""
        self._state.deny = deny

    def set_sign_id_token_badly(self, bad: bool) -> None:
        """Script the ``/token`` id_token signature: when ``bad`` is true the id_token
        the token endpoint returns is signed with the off-JWKS key (same kid), so the
        login callback resolves the published key and then fails the signature check —
        the bad-signature id_token negative driven through the full authorize flow."""
        self._sign_id_token_badly = bad

    def jwks(self) -> dict[str, Any]:
        """The published JWK set (public key only), as ``/jwks`` serves it."""
        return {"keys": [self._key.as_dict(private=False)]}

    def mint_jwt(
        self,
        *,
        sub: str | None = None,
        aud: str | None = None,
        expired: bool = False,
        bad_signature: bool = False,
        nonce: str | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Mint a signed RS256 JWT directly (no HTTP round trip).

        ``sub``/``aud`` default to the construction subject/client; ``expired`` puts
        ``exp`` in the past; ``bad_signature`` signs with an off-JWKS key of the same
        kid (so verification resolves a key and then fails the signature check).
        The assert-the-negative OIDC specs use this to produce good AND bad tokens."""
        return self._sign(
            sub=sub if sub is not None else self.subject,
            aud=aud if aud is not None else self._audience,
            expired=expired,
            bad_signature=bad_signature,
            nonce=nonce,
            extra_claims=extra_claims,
        )

    def _sign(
        self,
        *,
        sub: str,
        aud: str,
        expired: bool,
        bad_signature: bool,
        nonce: str | None,
        extra_claims: dict[str, Any] | None,
    ) -> str:
        now = int(time.time()) + self._clock_offset_seconds
        exp = now - _ID_TOKEN_TTL_SECONDS if expired else now + _ID_TOKEN_TTL_SECONDS
        claims: dict[str, Any] = {
            "iss": self.base_url,
            "aud": aud,
            "sub": sub,
            "email": self.email,
            "iat": now,
            "exp": exp,
        }
        if nonce is not None:
            claims["nonce"] = nonce
        if extra_claims:
            claims.update(extra_claims)
        key = self._bad_key if bad_signature else self._key
        return jose_jwt.encode({"alg": "RS256", "kid": _IDP_KID}, claims, key)

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        async def discovery() -> JSONResponse:
            base = self.base_url
            return JSONResponse(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "userinfo_endpoint": f"{base}/userinfo",
                    "jwks_uri": f"{base}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                }
            )

        @app.get("/jwks")
        async def jwks() -> JSONResponse:
            return JSONResponse(self.jwks())

        @app.get("/authorize")
        async def authorize(redirect_uri: str, state: str = "", nonce: str = "") -> RedirectResponse:
            sep = "&" if "?" in redirect_uri else "?"
            if self._state.deny:
                # The user refused consent: return the OAuth error redirect the
                # callback page relays back to the app, never a code.
                return RedirectResponse(url=f"{redirect_uri}{sep}error=access_denied&state={state}", status_code=302)
            code = f"code-{uuid.uuid4().hex[:8]}"
            # Remember the nonce so /token can bind it into the id_token — the OIDC
            # replay binding the login callback verifies against its stored nonce.
            self._state.codes[code] = {"nonce": nonce}
            return RedirectResponse(url=f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)

        @app.post("/token")
        async def token(request: Request) -> JSONResponse:
            grant = await _form_or_json(request)
            grant_type = grant.get("grant_type")
            if grant_type == "refresh_token":
                self._state.refresh_count += 1
            access = f"at-{uuid.uuid4().hex}"
            refresh = f"rt-{uuid.uuid4().hex}"
            self._state.issued_access_tokens.append(access)
            # Echo the authorize nonce (single-use) into the signed id_token; a
            # refresh grant carries no code and so no nonce.
            code = grant.get("code")
            record = self._state.codes.pop(code, None) if isinstance(code, str) else None
            nonce = record.get("nonce") if record else None
            id_token = self._sign(
                sub=self.subject,
                aud=self._audience,
                expired=False,
                bad_signature=self._sign_id_token_badly,
                nonce=nonce or None,
                extra_claims=None,
            )
            return JSONResponse(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": id_token,
                    "token_type": "Bearer",
                    "expires_in": self._state.expires_in,
                    "scope": "read",
                }
            )

        @app.get("/userinfo")
        async def userinfo() -> JSONResponse:
            return JSONResponse({"sub": self.subject, "email": self.email})

        # Out-of-process control: a Playwright spec in a separate Node process flips the
        # denial knob over HTTP (the in-process ``set_deny`` cannot cross the boundary).
        @app.post("/_deny")
        async def deny_authorize() -> JSONResponse:
            self.set_deny(True)
            return JSONResponse({"deny": True})

        @app.post("/_allow")
        async def allow_authorize() -> JSONResponse:
            self.set_deny(False)
            return JSONResponse({"deny": False})

        return app


async def _form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form.items())


# ---- channel provider stubs ---------------------------------------------
#
# Recording in-process stubs of the three channel providers' APIs, each a thread-hosted
# FastAPI on an allocated loopback port (like ``TargetServer``). Each records the plugin's
# outbound calls, mints the id the real provider would, and carries a helper that builds a
# genuinely signed inbound request so the plugin's REAL signature-verification runs against
# it — never a bypass. Any unexpected path is a loud 500, like the LlmStub unscripted rule.


class FakeTelegram:
    """A recording stub of the Telegram Bot API.

    Serves ``sendMessage`` (minting a monotonically increasing ``message_id``) and
    ``setWebhook`` (which the telegram plugin's ``on_startup`` hook calls, so boot
    succeeds against the stub — both replicas fire it, one record each). Any other
    path answers a loud 500."""

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
            self.sent.append(
                {
                    "token": token,
                    "chat_id": str(payload.get("chat_id")),
                    "text": payload.get("text", ""),
                    "reply_markup": payload.get("reply_markup"),
                    "message_id": message_id,
                }
            )
            return JSONResponse(
                {"ok": True, "result": {"message_id": message_id, "chat": {"id": payload.get("chat_id")}}}
            )

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
            form = dict((await request.form()).items())
            sid = f"SM{uuid.uuid4().hex}"
            self.messages.append(
                {
                    "account_sid": account_sid,
                    "to": form.get("To"),
                    "from": form.get("From"),
                    "body": str(form.get("Body", "")),
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


@dataclass(frozen=True)
class SignedInbound:
    """A ready-to-POST inbound request the channel stubs synthesize: the headers
    (carrying the genuine signature) and the exact raw body bytes the signature
    was computed over."""

    headers: dict[str, str]
    body: bytes


def _install_catch_all(app: FastAPI, provider: str) -> None:
    """Answer any unscripted path with a loud 500 — an unexpected provider call
    must fail the test, never be silently absorbed (the LlmStub unscripted rule)."""

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def unexpected(path: str, request: Request) -> JSONResponse:
        return JSONResponse(
            {"error": f"fake_{provider}: unexpected {request.method} /{path}"},
            status_code=500,
        )


# ---- Stripe API stub ----------------------------------------------------
#
# A loopback stub of the two Stripe REST endpoints the payment tools call — Checkout
# Session create and list — modelled on the channel stubs above but disjoint from them.
# Every session it mints and every field it echoes is what a real Stripe integration would
# produce, so a leg that passes against it could pass against Stripe. No run-time call from
# the SUT reaches a real Stripe host: the payments stack's ``STRIPE_API_BASE`` points here,
# and the webhook deliveries are locally HMAC-signed by the test with the topic's secret.


class FakeStripe:
    """A recording loopback stub of Stripe's Checkout Session REST API.

    Serves ``POST /v1/checkout/sessions`` (mint a session ``status=open`` /
    ``payment_status=unpaid``, echoing the create's amount, currency and decoded metadata)
    and ``GET /v1/checkout/sessions`` (list ``status=complete`` sessions created at or after
    ``created[gte]``, honouring the ``starting_after`` cursor and ``limit`` server-side). Any
    other path answers a loud 500. A session is completed only through
    :meth:`complete_payment`, which flips BOTH ``status`` and ``payment_status`` and mints the
    ``payment_intent`` — the two fields decide different things (whether the session lists,
    and whether it is answered), so no test reaches into a session dict and flips one."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        # Sessions by id, in creation order (the cursor's stable ordering).
        self._sessions: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        # Deterministic Stripe idempotency: a retried create for the same Idempotency-Key
        # returns the stored session instead of minting a second, as Stripe does.
        self._by_idempotency: dict[str, str] = {}
        # Recorded list-request query params (each call), so the paging leg can assert the
        # second request of a run carried ``starting_after``.
        self.list_requests: list[dict[str, str]] = []
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def api_base_url(self) -> str:
        """The value ``STRIPE_API_BASE`` points at (the tools address
        ``{api_base_url}/v1/checkout/sessions``)."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def reset(self) -> None:
        """Clear recorded requests AND stored sessions. Called between the two payment
        tests, which share one module-scoped stack, so the second never inherits the
        first's sessions."""
        self._sessions.clear()
        self._order.clear()
        self._by_idempotency.clear()
        self.list_requests.clear()

    @property
    def session_count(self) -> int:
        """How many sessions the stub has minted — the injection leg asserts a rejected
        create reached no Stripe call, so this never moved."""
        return len(self._sessions)

    def complete_payment(self, session_id: str) -> dict[str, Any]:
        """Complete a session the way a real payment does: ``status=complete`` AND
        ``payment_status=paid``, minting the ``payment_intent``. Both flips are required —
        ``status`` decides whether the session comes back from the list at all,
        ``payment_status`` decides whether it is answered."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"FakeStripe has no session {session_id!r} to complete")
        session["status"] = "complete"
        session["payment_status"] = "paid"
        session["payment_intent"] = f"pi_test_{uuid.uuid4().hex}"
        return session

    def session(self, session_id: str) -> dict[str, Any]:
        """The stored session dict (the test reads its captured fields to forge or sign a
        delivery from the real session)."""
        return self._sessions[session_id]

    def session_ids(self) -> list[str]:
        """The minted session ids in creation order — the test diffs this before/after
        opening an ask to learn which session the composed tool created for it."""
        return list(self._order)

    def _mint_session(self, *, amount_total: int, currency: str, metadata: dict[str, str]) -> dict[str, Any]:
        session_id = f"cs_test_{uuid.uuid4().hex}"
        session = {
            "id": session_id,
            "object": "checkout.session",
            "status": "open",
            "payment_status": "unpaid",
            "payment_intent": None,
            "amount_total": amount_total,
            "currency": currency,
            "livemode": False,
            # Raw (unflattened) shape: the reconciler reads ``customer_details.email`` and
            # build_answer_payload's fallback branch runs; ``customer_email`` stays absent.
            "customer_details": {"email": None},
            "metadata": metadata,
            "created": int(time.time()),
            "url": f"http://{self.host}:{self.port}/pay/{session_id}",
        }
        self._sessions[session_id] = session
        self._order.append(session_id)
        return session

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/checkout/sessions")
        async def create_session(request: Request) -> JSONResponse:
            form = await request.form()
            pairs = list(form.multi_items())
            values = dict(pairs)
            idempotency_key = request.headers.get("idempotency-key")
            if idempotency_key is not None and idempotency_key in self._by_idempotency:
                return JSONResponse(self._sessions[self._by_idempotency[idempotency_key]])
            # amount + currency come off the single line item; a create with neither is a
            # malformed request and 400s (never a silent default that would hide a bad body).
            try:
                amount_total = int(str(values["line_items[0][price_data][unit_amount]"]))
                currency = str(values["line_items[0][price_data][currency]"])
            except (KeyError, ValueError):
                return JSONResponse({"error": {"message": "fake_stripe: missing line-item amount/currency"}}, 400)
            # Metadata is OPTIONAL: a create with no ``metadata[<key>]`` params (the page
            # fillers) stores an empty map rather than being rejected.
            metadata = {
                key[len("metadata[") : -1]: str(value)
                for key, value in pairs
                if key.startswith("metadata[") and key.endswith("]")
            }
            session = self._mint_session(amount_total=amount_total, currency=currency, metadata=metadata)
            if idempotency_key is not None:
                self._by_idempotency[idempotency_key] = session["id"]
            return JSONResponse(session)

        @app.get("/v1/checkout/sessions")
        async def list_sessions(request: Request) -> JSONResponse:
            params = dict(request.query_params)
            self.list_requests.append(params)
            created_gte = int(params.get("created[gte]", "0"))
            status = params.get("status")
            limit = int(params.get("limit", "10"))
            starting_after = params.get("starting_after")
            # Server-side narrowing: honour the status filter and the created[gte] floor, in
            # the stable creation order the cursor pages through.
            candidates = [
                self._sessions[sid]
                for sid in self._order
                if (status is None or self._sessions[sid]["status"] == status)
                and self._sessions[sid]["created"] >= created_gte
            ]
            if starting_after is not None:
                ids = [s["id"] for s in candidates]
                candidates = candidates[ids.index(starting_after) + 1 :] if starting_after in ids else []
            page = candidates[:limit]
            has_more = len(candidates) > limit
            return JSONResponse({"object": "list", "url": "/v1/checkout/sessions", "has_more": has_more, "data": page})

        _install_catch_all(app, "stripe")
        return app
