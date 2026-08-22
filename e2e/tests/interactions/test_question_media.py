"""Display-only question media rides ask -> store-by-reference -> paged list + tail.

An ``ask_user`` question may carry optional ``media`` (images and/or links) shown
WITH the question in the inbox. These specs prove the wire end to end against the
real booted stack: a ``data:image`` is stored BY REFERENCE (the durable record and
the frame carry a same-origin ``/api/interactions/media/<id>`` url, never the inline
bytes) and the served-media door returns the bytes with the right mime; the media
list reaches the ADD frame EXACTLY on BOTH the paged list door and the live tail;
invalid media is a loud pre-persist failure that writes zero state (the paged list
never shows it); an absent media argument means an absent frame key; and media is
display-only — it never changes the answer round-trip. Media values are hermetic (a
tiny inline PNG plus urls the SUT and the tests never dereference) — the wire and
the store-by-reference gate are under test, not third-party availability.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
# A 1x1 PNG data URI: stored by reference (the durable record keeps a served url).
_DATA_IMAGE = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()

# Three items exercising every accepted branch: a remote https image (captioned),
# an inline ``data:image`` (captioned, stored by reference), and a captionless link.
_MEDIA: list[dict[str, object]] = [
    {"kind": "image", "url": "https://example.com/red-mug.png", "caption": "Red mug photo"},
    {"kind": "image", "url": _DATA_IMAGE, "caption": "Red mug chart"},
    {"kind": "link", "url": "https://example.com/red-mug"},
]

_MEDIA_REF_RE = re.compile(r"/api/interactions/media/[A-Za-z0-9_-]{43}")


def _assert_media_stored_by_reference(media: list[dict[str, Any]]) -> str:
    """Assert the frame's media list EXACTLY: the https image and the link pass through
    unchanged, and the ``data:image`` is a same-origin served reference (never the inline
    bytes). Returns the served-media url path for the caller to fetch."""
    assert media[0] == {"kind": "image", "url": "https://example.com/red-mug.png", "caption": "Red mug photo"}
    assert media[1]["kind"] == "image"
    assert media[1]["caption"] == "Red mug chart"
    assert _MEDIA_REF_RE.fullmatch(media[1]["url"]), media[1]["url"]
    assert media[2] == {"kind": "link", "url": "https://example.com/red-mug"}
    return media[1]["url"]


async def _ask(stack: TaiStack, question: str, media: list[dict[str, object]] | None) -> object:
    """Drive the builtin ``ask_user`` tool over MCP on replica A; the call PARKS
    until the question is answered, so its awaited result is the human's answer."""
    arguments: dict[str, object] = {"question": question}
    if media is not None:
        arguments["media"] = media
    async with stack.mcp(port=stack.port_a) as mcp:
        # A boot-time ask races the ~2s self-resync gate (a retriable ``reloading``);
        # poll past it so the call parks on the real question, not the reload gate.
        result = await mcp.call_tool("ask_user", arguments, retry_on_reloading=True)
    return result.data


def _add_frame_data(frame: str) -> dict[str, Any] | None:
    """Parse one SSE frame's ``data:`` JSON if it is an ADD frame (it carries a
    ``question``); a keepalive or terminal frame (neither carries a question) returns
    ``None``."""
    for line in frame.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) and "question" in data else None
    return None


async def _iter_frames(response: httpx.Response) -> AsyncIterator[str]:
    """Yield complete SSE frames (``\\n\\n``-terminated) off a live stream."""
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            yield frame


async def _list_pending(stack: TaiStack, port: int, question: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    """Read the paged pending-list door and return the add-frame ``data`` for
    ``question`` if it is pending, else ``None`` (the paged pending-list door is the
    initial-load surface)."""
    url = f"http://{stack.host}:{port}/api/interactions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params={"page": 1, "pageSize": 200})
        resp.raise_for_status()
        page = resp.json()["data"]
        for item in page["items"]:
            if item.get("question") == question:
                return item
    return None


async def _fetch_media(stack: TaiStack, port: int, url_path: str, *, timeout: float = 3.0) -> httpx.Response:
    """GET the served-media capability url (the id IS the secret; no auth needed)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(f"http://{stack.host}:{port}{url_path}")


async def _next_add_frame(frames: AsyncIterator[str], question: str, *, deadline: float) -> dict[str, Any]:
    """Return the next add-frame ``data`` for ``question`` off the live tail."""

    async def run() -> dict[str, Any]:
        async for frame in frames:
            data = _add_frame_data(frame)
            if data is not None and data.get("question") == question:
                return data
        raise AssertionError(f"interactions stream ended before an add frame for {question!r}")

    return await asyncio.wait_for(run(), timeout=deadline)


async def _answer_and_wait(stack: TaiStack, interaction_id: str, ask_task: asyncio.Task[object], answer: str) -> object:
    """Answer the pending interaction via replica B and return the parked tool
    result — the caller asserts the answer round-tripped."""
    api_b = stack.api(port=stack.port_b)
    await api_b.post(f"/api/interactions/{interaction_id}/answer", json={"answer": answer})
    return await asyncio.wait_for(ask_task, timeout=10.0)


async def test_media_round_trips_through_the_list_door(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    ask_task = asyncio.create_task(_ask(replicas_stack, question, _MEDIA))
    try:
        data = await wait_for_async(
            lambda: _list_pending(replicas_stack, replicas_stack.port_b, question),
            deadline=8.0,
            message=f"media question {question!r} never appeared in the paged list on B",
        )
        assert data is not None
        # The data:image is stored by reference; the https image and link are unchanged.
        media_url = _assert_media_stored_by_reference(data["media"])
        # The served-media door returns the exact bytes with the right mime.
        served = await _fetch_media(replicas_stack, replicas_stack.port_b, media_url)
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"
        assert served.content == _PNG_BYTES
        answer = await _answer_and_wait(replicas_stack, data["interaction_id"], ask_task, "list-answer")
    finally:
        if not ask_task.done():
            ask_task.cancel()
    # Media is display-only: the answer round-trips unchanged with media present.
    assert "list-answer" in json.dumps(answer)


async def test_media_round_trips_on_the_live_tail(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    url = f"http://{replicas_stack.host}:{replicas_stack.port_b}/api/interactions/stream"
    async with httpx.AsyncClient(timeout=20.0) as client, client.stream("GET", url) as response:
        # Entering ``client.stream`` has already received the response headers, so the
        # server captured the tail cursor before the ask below — the later add is
        # guaranteed on the tail. Tail-only: the stream carries no backlog, so that add
        # frame is unambiguously a live-tail frame.
        assert response.status_code == 200, response
        frames = _iter_frames(response)
        ask_task = asyncio.create_task(_ask(replicas_stack, question, _MEDIA))
        try:
            data = await _next_add_frame(frames, question, deadline=8.0)
            _assert_media_stored_by_reference(data["media"])
            answer = await _answer_and_wait(replicas_stack, data["interaction_id"], ask_task, "live-tail-answer")
        finally:
            if not ask_task.done():
                ask_task.cancel()
    assert "live-tail-answer" in json.dumps(answer)


async def test_invalid_media_fails_loudly_and_writes_zero_state(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # Each invalid class is rejected at the contract BEFORE any state is written:
    # the tool surfaces an error naming the media rule, and the paged list never shows
    # the question.
    cases: list[tuple[str, list[dict[str, object]], str]] = [
        (uniq("q_scheme"), [{"kind": "image", "url": "javascript:alert(1)"}], "image media url"),
        (
            uniq("q_overcap"),
            [{"kind": "link", "url": f"https://example.com/{i}"} for i in range(51)],
            "at most 50 items",
        ),
    ]
    for question, media, needle in cases:
        async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
            # Retry past a boot-time ``reloading`` gate; the invalid-media rejection is
            # a non-reload error, so it is returned at once (never retried).
            result = await mcp.call_tool(
                "ask_user", {"question": question, "media": media}, raise_on_error=False, retry_on_reloading=True
            )
        assert result.is_error, f"expected invalid media to reject {question!r}"
        text = "".join(getattr(block, "text", "") for block in (result.content or []))
        assert needle in text, f"error for {question!r} did not name the media rule: {text!r}"
        # Zero state: the rejected question never reaches the paged list.
        leaked = await _list_pending(replicas_stack, replicas_stack.port_b, question, timeout=5.0)
        assert leaked is None, f"rejected media question {question!r} leaked a list frame: {leaked!r}"


async def test_a_question_without_media_emits_no_media_key(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")
    ask_task = asyncio.create_task(_ask(replicas_stack, question, None))
    try:
        data = await wait_for_async(
            lambda: _list_pending(replicas_stack, replicas_stack.port_b, question),
            deadline=8.0,
            message=f"media-less question {question!r} never appeared in the paged list on B",
        )
        assert data is not None
        # Absent media is an absent key, not an empty list — the additive-wire pin.
        assert "media" not in data
        answer = await _answer_and_wait(replicas_stack, data["interaction_id"], ask_task, "no-media-answer")
    finally:
        if not ask_task.done():
            ask_task.cancel()
    assert "no-media-answer" in json.dumps(answer)
