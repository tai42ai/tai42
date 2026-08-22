"""The versioned JSONL exec protocol between the adapter and the in-session runner.

Frames are single-line JSON over the interactive exec handle, versioned (``{"v":1,...}``);
stderr is diagnostics only (never parsed). The ADAPTER is the ONLY parser — an unknown frame
version or type is a loud :class:`ProtocolError`, never a silent skip.

* **Down** (adapter -> runner, via stdin): ``start`` / ``answer`` / ``tool_result`` / ``stop``.
* **Up** (runner -> adapter, via stdout): ``hello`` / ``event`` / ``ask`` / ``tool_call`` /
  ``result`` / ``fatal``.

The runner reports its actual ``claude_agent_sdk`` version on the ``hello`` frame at STREAM
START; the adapter REJECTS a mismatch with :data:`CLAUDE_AGENT_SDK_VERSION` before trusting any
frame — the runtime gate that makes cross-repo pin drift impossible to ship silently. The
effective SDK ``session_id`` rides that same init frame (NOT the result frame), so it is
captured even on a turn that ends by interrupt/park.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

# The single protocol version both sides stamp/require. A frame carrying any other ``v`` is a
# loud protocol error.
PROTOCOL_VERSION = 1

# The pinned Claude Agent SDK version the runner payload is written against. Coordinated with
# PLAN_6's session-image ``claude-agent-sdk==<PIN>``; the ``hello`` mismatch makes drift loud.
CLAUDE_AGENT_SDK_VERSION = "0.1.0"


class ProtocolError(RuntimeError):
    """A malformed, unknown-version, or unknown-type frame, or a runner ``fatal`` — every
    protocol fault raises this rather than degrading to a partial outcome."""


# --- Down frames (adapter -> runner) -------------------------------------------------------


class StartFrame(BaseModel):
    """Kick off one turn: the options payload, the prompt, the requested-tool allowlist, the
    skills list, and the adapter's pinned SDK version (echoed back for the ``hello`` gate)."""

    v: int = PROTOCOL_VERSION
    type: Literal["start"] = "start"
    options: dict[str, Any]
    prompt: dict[str, Any]
    tool_names: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    sdk_version: str = CLAUDE_AGENT_SDK_VERSION


class AnswerFrame(BaseModel):
    """Deliver a human's answer to a blocked in-process ``ask_user`` handler."""

    v: int = PROTOCOL_VERSION
    type: Literal["answer"] = "answer"
    ask_id: str
    answer: Any = None
    is_error: bool = False


class ToolResultFrame(BaseModel):
    """Return one adapter-proxied platform tool call's result to its runner handler."""

    v: int = PROTOCOL_VERSION
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    result: Any = None
    is_error: bool = False


class StopFrame(BaseModel):
    """Interrupt the drive — a park or a cancel; the runner honors it mid-stream."""

    v: int = PROTOCOL_VERSION
    type: Literal["stop"] = "stop"
    reason: Literal["park", "cancel"] = "cancel"


# --- Up frames (runner -> adapter) ---------------------------------------------------------


class HelloFrame(BaseModel):
    """The init frame at stream start: the runner's ACTUAL SDK version and the effective SDK
    session id (captured here, before any turn work, so a first-turn park still records it)."""

    v: int = PROTOCOL_VERSION
    type: Literal["hello"] = "hello"
    sdk_version: str
    session_id: str


class EventFrame(BaseModel):
    """A typed SDK message dump the adapter maps to one contract stream event."""

    v: int = PROTOCOL_VERSION
    type: Literal["event"] = "event"
    event: dict[str, Any]


class AskFrame(BaseModel):
    """The runner's in-process ``ask_user`` tool blocked on a question; ``mode`` selects the
    sync/async wait discipline (the model chose it, default sync)."""

    v: int = PROTOCOL_VERSION
    type: Literal["ask"] = "ask"
    ask_id: str
    question: str
    mode: Literal["sync", "async"] = "sync"
    answer_format: str = "text"
    options: list[str] | None = None


class ToolCallFrame(BaseModel):
    """A proxy tool handler asking the adapter to run a platform tool under the turn identity."""

    v: int = PROTOCOL_VERSION
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ResultFrame(BaseModel):
    """The terminal frame: the outcome reason, usage/cost, and any structured result."""

    v: int = PROTOCOL_VERSION
    type: Literal["result"] = "result"
    terminal_reason: str
    subtype: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    result: Any = None
    is_structured: bool = False


class FatalFrame(BaseModel):
    """A constant-safe fatal error the runner could not recover from."""

    v: int = PROTOCOL_VERSION
    type: Literal["fatal"] = "fatal"
    message: str


UpFrame = Annotated[
    HelloFrame | EventFrame | AskFrame | ToolCallFrame | ResultFrame | FatalFrame,
    Field(discriminator="type"),
]

_UP_ADAPTER: TypeAdapter[Any] = TypeAdapter(UpFrame)


def dump_frame(frame: BaseModel) -> bytes:
    """Serialize one down-frame as a single newline-terminated JSON line (the on-wire unit)."""
    return (frame.model_dump_json() + "\n").encode("utf-8")


def parse_up_frame(line: str) -> Any:
    """Parse one up-frame line, raising :class:`ProtocolError` on any malformed input.

    A non-JSON line, a wrong protocol version, or an unknown frame type all raise loudly — the
    adapter never trusts a frame it cannot fully validate.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"runner emitted a non-JSON frame line: {line!r}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError(f"runner frame is not a JSON object: {line!r}")
    version = raw.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"runner frame protocol version {version!r} != adapter version {PROTOCOL_VERSION}")
    try:
        return _UP_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise ProtocolError(f"runner emitted an unknown or malformed frame: {raw.get('type')!r}") from exc
