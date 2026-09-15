"""Up-frame plumbing for ``claude_code``.

Iterate the runner's byte stream into parsed up-frames, drain a killed handle, and map an event /
terminal frame to a contract stream event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from tai42_contract.agent.events import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    StreamEvent,
    StructuredFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.sandbox import (
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_agents.claude_code.protocol import ProtocolError, ResultFrame, parse_up_frame


async def iter_up_frames(handle: Any) -> AsyncIterator[Any]:
    """Yield parsed up-frames off the exec handle's byte stream.

    Buffers whole JSON lines and ignores stderr (diagnostics). A ``SandboxStreamExit`` ends the
    stream.
    """
    buffer = bytearray()
    async for chunk in handle.output:
        if isinstance(chunk, SandboxStreamExit):
            break
        if not (isinstance(chunk, SandboxStreamChunk)):
            raise AssertionError  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
        if chunk.stream != "stdout":
            continue
        buffer.extend(chunk.data)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            text = line.decode("utf-8", "replace").strip()
            if text:
                yield parse_up_frame(text)


async def drain_handle(handle: Any) -> None:
    """Await the killed handle's stream to completion so no runner code is still mid-drive."""
    try:
        async for _ in handle.output:
            pass
    except (SandboxExecTimeoutError, ProtocolError):
        pass


def map_event(event: dict[str, Any], text_parts: list[str]) -> StreamEvent | None:
    """Map one runner ``event`` payload to a contract stream event (or ``None`` to skip)."""
    kind = event.get("kind")
    if kind == "text":
        text = event.get("text", "")
        text_parts.append(text)
        return MessageDelta(text=text)
    if kind == "thinking":
        text = event.get("text", "")
        if not text.strip():
            return None
        return ReasoningStep(text=text)
    if kind == "tool_use":
        return ToolCallStep(tool=event.get("name", ""), args=event.get("input", {}) or {}, call_id=event.get("id", ""))
    if kind == "tool_result":
        return ToolResultStep(
            tool="", call_id=event.get("id", ""), result=event.get("content"), is_error=bool(event.get("is_error"))
        )
    return None


def terminal_event(frame: ResultFrame, text_parts: list[str]) -> StreamEvent:
    """The contract terminal for a result ``frame``; raises ``ProtocolError`` on a non-success reason.

    A structured result yields a :class:`StructuredFinal`, else the frame's text (or the joined
    ``text_parts``) yields a :class:`MessageFinal`.
    """
    if frame.terminal_reason not in {"completed", "success"}:
        raise ProtocolError(f"runner terminated with reason {frame.terminal_reason!r} (subtype {frame.subtype!r})")
    if frame.is_structured and frame.result is not None:
        return StructuredFinal(data=frame.result)
    if isinstance(frame.result, str):
        return MessageFinal(text=frame.result)
    return MessageFinal(text="".join(text_parts))
