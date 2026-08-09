"""Client for the web channel plugin's public chat doors (``/api/channels/web``).

The web channel has no vendor, so there is no recording stub to point it at: its own
public doors ARE the medium. A visitor holds exactly ONE credential — the secret token
the chat page mints into the ``tai_web_session`` cookie — and the durable conversation
ADDRESS behind it (the ``visitor_id``) is never sent to the client: the page registers
the pair server-side and every door resolves the cookie through that registration.

So a spec that must NAME the address — an ask's ``"<identity>:<visitor id>"`` recipient,
or the transcript key it censuses — reads it out of the registration in the plugin's own
store (:func:`registered_visitor_id`), never off the cookie. Both the channels suite (the
ask/answer half, on the backendless channel stack) and the bridge suite (the full message
round trip) drive the same doors, so the door shapes live here once.

Every method returns the raw ``httpx`` response — a spec asserts the status itself, and
the fail-closed negatives (no cookie, an unregistered token, another visitor's session)
are driven by passing ``cookies``.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import cast

import httpx
import redis as redis_lib

SESSION_COOKIE = "tai_web_session"

# The plugin's own key shapes in its store: a session registration holds the visitor id a
# cookie token resolves to, and a transcript stream is keyed by the (identity, visitor id)
# pair. A spec reads both directly when it needs the server-side fact the doors withhold.
SESSION_KEY_PREFIX = "channel:web:session:"
TRANSCRIPT_KEY_PREFIX = "channel:web:transcript:"

_BACKLOG_DONE = "chat.backlog_done"


def mint_unregistered_token() -> str:
    """A session cookie token in the plugin's minted shape (32 CSPRNG bytes, urlsafe)
    that was never REGISTERED — the negatives' point: it passes the cookie's format
    gate and still resolves to no visitor, so it addresses no conversation and can
    open none."""
    return secrets.token_urlsafe(32)


def _store(store_url: str) -> redis_lib.Redis:
    return redis_lib.Redis.from_url(store_url, decode_responses=True)


def registered_visitor_id(store_url: str, token: str) -> str:
    """The visitor id the server registered ``token`` against — the conversation address
    the cookie stands for, which the doors never disclose. An unregistered token raises:
    a spec that asked for an address must never silently proceed without one."""
    client = _store(store_url)
    try:
        # redis-py types every command as the sync/async union; this client is sync.
        raw = cast(str | None, client.get(f"{SESSION_KEY_PREFIX}{token}"))
    finally:
        client.close()
    if raw is None:
        raise AssertionError(f"no web session registration for the cookie token {token[:8]}...")
    visitor_id = json.loads(raw)["visitor_id"]
    assert isinstance(visitor_id, str), f"malformed web session registration: {raw!r}"
    assert visitor_id, f"web session registration carries a blank visitor_id: {raw!r}"
    return visitor_id


def transcript_keys(store_url: str, identity: str) -> set[str]:
    """Every transcript stream key under ``identity`` right now — the census an
    "opened no conversation" assertion deltas against."""
    client = _store(store_url)
    try:
        return set(cast(Iterable[str], client.scan_iter(match=f"{TRANSCRIPT_KEY_PREFIX}{identity}:*")))
    finally:
        client.close()


async def get_chat_page(
    base_url: str,
    identity: str,
    *,
    query: dict[str, str] | list[tuple[str, str]] | str | None = None,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET the chat page and return the RAW response — no cookie is asserted.

    :meth:`WebChatClient.open_page` raises when the door mints no cookie, so it cannot drive
    the refusal legs (a 400 bounds page and a 403 gate page mint none by design): this helper
    is how those legs read the page-door refusal — its status and its ``tai42-refusal-code``
    meta. ``query`` passes through to httpx, so a list of pairs expresses a DUPLICATE key
    (``[("x", "1"), ("x", "2")]``) the door must refuse; ``cookies`` presents an existing
    session (the admitted-reload legs); ``headers`` sets navigation hints (a non-``document``
    ``Sec-Fetch-Dest`` is the non-navigation the door must refuse BEFORE the gate)."""
    # httpx no longer honours per-request cookies (the jar lives on the client), so an
    # existing session is presented by seating it on the client itself.
    async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
        return await client.get(
            f"{base_url.rstrip('/')}/api/channels/web/chat/{identity}",
            params=query,
            headers=headers,
        )


@dataclass
class WebChatClient:
    """One visitor of one web route: a base origin, the route identity, the cookie token
    the visitor holds, and the server-side ``visitor_id`` that token is registered
    against — the conversation's address, known here only because a spec read the
    registration."""

    base_url: str
    identity: str
    token: str
    visitor_id: str

    @classmethod
    async def open_page(
        cls, base_url: str, identity: str, *, store_url: str, query: dict[str, str] | None = None
    ) -> tuple[WebChatClient, httpx.Response]:
        """GET the chat page and adopt the session cookie the server minted, exactly as a
        first-time visitor's browser does, then read the visitor id that token was
        registered against. Raises when the door mints no cookie — without a session
        there is no conversation to open.

        ``query`` appends URL query parameters to the page load. Two are the browser's own
        coordinates the door consumes and never stores: the ``pair`` invite code (the bundle
        submits it once as the visitor's first message) and the ``tai_entry`` gate code. Every
        OTHER name is a link param captured with this session and delivered to the turn's tool
        payload under ``.params`` — the shell HTML is identical either way; the caller stands in
        for the bundle by sending whatever the query implies."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/channels/web/chat/{identity}", params=query)
        token = response.cookies.get(SESSION_COOKIE)
        if token is None:
            raise AssertionError(
                f"the chat page for {identity!r} set no {SESSION_COOKIE} cookie "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )
        client_obj = cls(
            base_url=base_url.rstrip("/"),
            identity=identity,
            token=token,
            visitor_id=registered_visitor_id(store_url, token),
        )
        return client_obj, response

    @property
    def cookies(self) -> dict[str, str]:
        return {SESSION_COOKIE: self.token}

    @property
    def recipient(self) -> str:
        """The composite address a web ask or a ``notify_user`` names this visitor by —
        the pair the channel splits back into the transcript key."""
        return f"{self.identity}:{self.visitor_id}"

    @property
    def transcript_key(self) -> str:
        """The plugin's transcript stream key for this visitor's pair — the store a spec
        censuses directly when it needs the write rather than the read."""
        return f"{TRANSCRIPT_KEY_PREFIX}{self.identity}:{self.visitor_id}"

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/channels/web{path}"

    def _client(self, timeout: float, cookies: dict[str, str] | None) -> httpx.AsyncClient:
        """A one-shot client carrying the visitor's cookie jar. httpx wants the jar on the
        client, not the request, so every door call gets its own — which also keeps a
        negative's substituted (or absent) cookie from leaking into the next call."""
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout), cookies=self.cookies if cookies is None else cookies)

    async def asset(self, name: str) -> httpx.Response:
        """GET one built bundle file by the exact name the page's shell links."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.get(self._url(f"/assets/{name}"))

    async def send(
        self, text: str, *, identity: str | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        """POST one visitor message. The address is taken from the cookie's registration
        alone — the body names only the route identity and the text, and ``identity``
        overrides the route name for the refusal legs."""
        body = {"identity": self.identity if identity is None else identity, "text": text}
        async with self._client(15.0, cookies) as client:
            return await client.post(self._url("/messages"), json=body)

    async def answer(
        self, interaction_id: str, value: object, *, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        async with self._client(15.0, cookies) as client:
            return await client.post(self._url(f"/questions/{interaction_id}/answer"), json={"answer": value})

    async def rotate(
        self,
        *,
        store_url: str,
        identity: str | None = None,
        entry_code: str | None = None,
        cookies: dict[str, str] | None = None,
    ) -> tuple[WebChatClient | None, httpx.Response]:
        """POST the rotate door — the visitor's "new conversation" — and adopt the fresh
        session it mints. Returns ``(client, response)``: the client is a new
        :class:`WebChatClient` bound to the minted token's own ``visitor_id``, or ``None`` when
        the door minted no cookie (a gated route refusing a rotation with no live code). A
        rotation mints a CLEAN registration, so the returned session carries no link params.

        ``entry_code`` carries the URL's ``tai_entry`` value the bundle sends on a gated route;
        it is NEVER logged (a code in CI logs outlives the run)."""
        target = self.identity if identity is None else identity
        body: dict[str, str] = {"identity": target}
        if entry_code is not None:
            body["entry_code"] = entry_code
        async with self._client(15.0, cookies) as client:
            response = await client.post(self._url("/session/rotate"), json=body)
        token = response.cookies.get(SESSION_COOKIE)
        if token is None:
            return None, response
        fresh = type(self)(
            base_url=self.base_url,
            identity=target,
            token=token,
            visitor_id=registered_visitor_id(store_url, token),
        )
        return fresh, response

    @contextlib.asynccontextmanager
    async def hold_stream(self, *, deadline: float = 20.0) -> AsyncIterator[httpx.Response]:
        """Open the SSE door and HOLD it open for the block's duration, yielding the
        response with its status already known.

        The stream-slot cap is counted per open stream, so proving it needs streams that
        stay open; a refused open is a plain JSON body, read here so the caller can assert
        on it without touching the stream again.

        An admitted stream is held only once its FIRST FRAME has arrived: the server takes
        the slot inside the SSE generator's body, which runs after the response start the
        status here comes from, so a caller that opened on the status alone could open the
        next stream against a slot not yet counted. Reading one frame is the arrival the
        slot is guaranteed by.

        That read's chunk iterator is then held for the whole block: an ABANDONED httpx
        chunk iterator is closed when it is collected, and closing it tears down the
        connection under this response — the held stream would end and hand its slot
        straight back."""
        async with (
            self._client(deadline, None) as client,
            client.stream("GET", self._url(f"/stream?identity={self.identity}")) as response,
        ):
            if response.status_code != 200:
                await response.aread()
                yield response
                return
            # httpx types the chunk iterator as a plain iterator; it is an async generator,
            # and closing it explicitly at the end of the block is what keeps its teardown
            # inside the hold rather than at some later collection.
            chunks = cast("AsyncGenerator[str]", response.aiter_text())
            async with contextlib.aclosing(chunks):
                await _await_first_frame(chunks)
                yield response

    async def frames(
        self,
        *,
        until: Callable[[str, dict], bool] | None = None,
        deadline: float = 12.0,
    ) -> list[tuple[str, dict]]:
        """Open the SSE door and collect its frames as ``(event, data)`` pairs.

        With no ``until`` this is a REPLAY read: it returns the backlog at the
        ``chat.backlog_done`` marker. With ``until`` it is an AWAIT read: the predicate is
        applied to EVERY frame, backlog and live tail alike, and the collected frames are
        returned at the first one it accepts. Judging the backlog too is what lets a spec
        start the awaited write concurrently with the open: a frame written before the
        stream opened arrives in the backlog and still satisfies the wait, where a
        live-only predicate would wait out the deadline for a frame already delivered.
        (The plugin's own cursor capture guarantees something narrower: the tail cursor is
        taken before the backlog read, so no frame falls BETWEEN the two.)

        The stream is the page's only read path, so a spec proving replay or delivery comes
        through here and not the store behind it. A deadline reached without the awaited
        frame raises — a short read is never returned as an absence."""
        collected: list[tuple[str, dict]] = []
        url = self._url(f"/stream?identity={self.identity}")
        async with (
            self._client(deadline, None) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                await response.aread()
                raise AssertionError(f"web stream door refused: HTTP {response.status_code}: {response.text[:500]}")
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    event, data = _parse_frame(raw)
                    if event is None:
                        continue  # a keepalive comment carries no event line
                    if event == _BACKLOG_DONE:
                        if until is None:
                            return collected
                        continue
                    collected.append((event, data))
                    if until is not None and until(event, data):
                        return collected
        raise AssertionError(f"web stream closed before {_BACKLOG_DONE if until is None else 'the awaited frame'}")


async def _await_first_frame(chunks: AsyncIterator[str]) -> None:
    """Read an open stream's chunks up to its first complete frame and stop there, leaving
    the iterator suspended for its holder to close.

    Always terminates on a live door: the backlog is followed by the
    ``chat.backlog_done`` marker, so a frame is due whether or not the transcript has
    entries. A stream that closes without one raises — an empty read is never taken for a
    held stream."""
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        if "\n\n" in buffer:
            return
    raise AssertionError("web stream closed before its first frame")


def _parse_frame(raw: str) -> tuple[str | None, dict]:
    """One SSE frame's event name and decoded payload; ``(None, {})`` for a comment frame."""
    event: str | None = None
    data: dict = {}
    for line in raw.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
    return event, data
