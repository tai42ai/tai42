"""Display-only question media rides ask -> store -> SSE.

An ``ask_user`` question may carry optional ``media`` (images and/or links) shown
WITH the question in the inbox. These specs prove the wire end to end against the
real booted stack: the media list reaches the SSE ``interaction.add`` frame EXACTLY
on both the backlog replay and the live tail; invalid media is a loud pre-persist
failure that writes zero state (no later stream frame carries it); an absent media
argument means an absent frame key; and media is display-only — it never changes
the answer round-trip. Media values are hermetic (a tiny inline PNG plus urls the
SUT and the tests never dereference) — the wire and the renderer gate are under
test, not third-party availability.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

# A 1x1 PNG data URI: renders with no network fetch (the CSP admits ``data:``).
_DATA_IMAGE = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

# Three items exercising every accepted branch: a remote https image (captioned),
# an inline ``data:image`` (captioned), and a captionless link. The add frame emits
# each item with ``exclude_none``, so a captionless item carries NO caption key —
# this list is therefore BOTH the argument sent and the exact frame expected.
_MEDIA: list[dict[str, object]] = [
    {"kind": "image", "url": "https://example.com/red-mug.png", "caption": "Red mug photo"},
    {"kind": "image", "url": _DATA_IMAGE, "caption": "Red mug chart"},
    {"kind": "link", "url": "https://example.com/red-mug"},
]


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
    ``question``); a keepalive, ``backlog_done`` sentinel, or terminal frame (none
    of which carry a question) returns ``None``."""
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


async def _scan_backlog(stack: TaiStack, port: int, question: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    """Open a FRESH SSE stream and read the backlog replay only (stopping at the
    ``backlog_done`` sentinel); return the add-frame ``data`` for ``question`` if it
    is pending, else ``None`` (backlog fully replayed, question absent — either it
    left zero state or has not persisted yet; the caller distinguishes those by
    retry vs. one-shot). A stream that ends BEFORE ``backlog_done`` is a server-side
    fault, raised loudly rather than reported as a false absence a zero-state
    assertion could pass on vacuously."""
    url = f"http://{stack.host}:{port}/api/interactions/stream"
    async with httpx.AsyncClient(timeout=timeout) as client, client.stream("GET", url) as response:
        async for frame in _iter_frames(response):
            data = _add_frame_data(frame)
            if data is not None and data.get("question") == question:
                return data
            if "backlog_done" in frame:
                return None
    raise AssertionError(f"interactions stream on port {port} closed before backlog_done; backlog state unknown")


async def _drain_to_backlog_done(frames: AsyncIterator[str], *, deadline: float) -> None:
    """Consume the backlog replay up to and including the ``backlog_done`` sentinel,
    so any following add frame is unambiguously a LIVE-tail frame."""

    async def run() -> None:
        async for frame in frames:
            if "backlog_done" in frame:
                return
        raise AssertionError("interactions stream ended before the backlog_done sentinel")

    await asyncio.wait_for(run(), timeout=deadline)


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


async def test_media_round_trips_through_the_backlog(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    ask_task = asyncio.create_task(_ask(replicas_stack, question, _MEDIA))
    try:
        data = await wait_for_async(
            lambda: _scan_backlog(replicas_stack, replicas_stack.port_b, question),
            deadline=8.0,
            message=f"media question {question!r} never appeared in the SSE backlog on B",
        )
        assert data is not None
        # The media list rides the add frame EXACTLY — order preserved, and the
        # captionless link item carries no caption key (``exclude_none``).
        assert data["media"] == _MEDIA
        answer = await _answer_and_wait(replicas_stack, data["interaction_id"], ask_task, "backlog-answer")
    finally:
        if not ask_task.done():
            ask_task.cancel()
    # Media is display-only: the answer round-trips unchanged with media present.
    assert "backlog-answer" in json.dumps(answer)


async def test_media_round_trips_on_the_live_tail(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    url = f"http://{replicas_stack.host}:{replicas_stack.port_b}/api/interactions/stream"
    async with httpx.AsyncClient(timeout=20.0) as client, client.stream("GET", url) as response:
        frames = _iter_frames(response)
        # Connect and drain the backlog FIRST; the question does not exist yet,
        # so the add frame that follows the ask is a genuine live-tail frame.
        await _drain_to_backlog_done(frames, deadline=8.0)
        ask_task = asyncio.create_task(_ask(replicas_stack, question, _MEDIA))
        try:
            data = await _next_add_frame(frames, question, deadline=8.0)
            assert data["media"] == _MEDIA
            answer = await _answer_and_wait(replicas_stack, data["interaction_id"], ask_task, "live-tail-answer")
        finally:
            if not ask_task.done():
                ask_task.cancel()
    assert "live-tail-answer" in json.dumps(answer)


async def test_invalid_media_fails_loudly_and_writes_zero_state(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # Each invalid class is rejected at the contract BEFORE any state is written:
    # the tool surfaces an error naming the media rule, and a subsequent backlog
    # read reaches backlog_done with no frame for the question.
    cases: list[tuple[str, list[dict[str, object]], str]] = [
        (uniq("q_scheme"), [{"kind": "image", "url": "javascript:alert(1)"}], "image media url"),
        (
            uniq("q_overcap"),
            [{"kind": "link", "url": f"https://example.com/{i}"} for i in range(9)],
            "at most 8 items",
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
        # Zero state: the rejected question never reaches the backlog.
        leaked = await _scan_backlog(replicas_stack, replicas_stack.port_b, question, timeout=5.0)
        assert leaked is None, f"rejected media question {question!r} leaked a backlog frame: {leaked!r}"


async def test_a_question_without_media_emits_no_media_key(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")
    ask_task = asyncio.create_task(_ask(replicas_stack, question, None))
    try:
        data = await wait_for_async(
            lambda: _scan_backlog(replicas_stack, replicas_stack.port_b, question),
            deadline=8.0,
            message=f"media-less question {question!r} never appeared in the SSE backlog on B",
        )
        assert data is not None
        # Absent media is an absent key, not an empty list — the additive-wire pin.
        assert "media" not in data
        answer = await _answer_and_wait(replicas_stack, data["interaction_id"], ask_task, "no-media-answer")
    finally:
        if not ask_task.done():
            ask_task.cancel()
    assert "no-media-answer" in json.dumps(answer)
