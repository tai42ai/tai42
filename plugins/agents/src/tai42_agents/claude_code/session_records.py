"""Durable per-session records ``claude_code`` keeps under ``.runner``.

The SDK session id a threaded turn resumes from, and the crash-after-terminal idempotence
record a resume drive writes so a redelivery re-produces the same terminal output without
re-driving the SDK.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from tai42_contract.agent.events import MessageFinal, StreamEvent, StructuredFinal
from tai42_contract.sandbox import SandboxSession

from tai42_agents.claude_code.protocol import ProtocolError, ResultFrame

# Workspace-relative path (rooted at ``session.workspace_path``) holding the persisted SDK
# session id for a threaded turn to resume from.
_SESSION_ID_PATH = ".runner/session_id"
# Crash-after-terminal idempotence records, one per resumed super-step, keyed by the
# ``compute_superstep_id`` of the resume's interaction ids. A resume drive writes its record on
# the clean terminal BEFORE reporting; a redelivered resume reads it and returns the SAME output
# without re-driving the SDK session. There is no LangGraph snapshot here, so this durable record
# IS the resume idempotence source (a durable-volume analogue of a checkpoint-snapshot guard).
_TERMINAL_DIR = ".runner/terminal"


class _TerminalRecord(BaseModel):
    """The durable crash-after-terminal idempotence record for one resumed super-step.

    Captures the exact terminal OUTPUT (a message ``text`` or a structured ``data``) plus the
    session id and usage; ``extra="forbid"`` so an in-session-forged record with stray keys fails
    validation and is treated as absent (the resume re-drives rather than honoring garbage).
    """

    model_config = ConfigDict(extra="forbid")
    superstep_id: str
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    structured: bool
    text: str | None = None
    data: Any = None


async def read_session_id(session: SandboxSession) -> str | None:
    """The persisted SDK session id for a threaded turn, or ``None`` when none is stored.

    Raises :class:`ProtocolError` when the stored record is present but malformed.
    """
    try:
        raw = await session.get_file(_SESSION_ID_PATH)
    except Exception:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        session_id = record["session_id"]
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
        raise ProtocolError("persisted .runner/session_id is malformed on a thread with prior turns") from exc
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("persisted .runner/session_id is malformed on a thread with prior turns")
    return session_id


async def persist_session_id(session: SandboxSession, session_id: str) -> None:
    """Persist ``session_id`` under ``.runner`` for a later threaded turn to resume from."""
    await session.put_file(_SESSION_ID_PATH, json.dumps({"session_id": session_id}).encode("utf-8"))


async def read_terminal_record(session: SandboxSession, superstep_id: str) -> _TerminalRecord | None:
    """Read + schema-validate the durable terminal record for a resumed super-step.

    Returns ``None`` when there is none (the common first-drive case) or it does not
    validate.

    UNTRUSTED UNTIL VERIFIED: the in-session Bash can write under ``.runner``, so the
    record is schema-validated and its ``superstep_id`` must match the one being resumed
    before it is honored — a forged record only controls THIS thread's own output. A
    malformed or mismatched record is treated as absent, so the resume re-drives rather
    than returning garbage.
    """
    try:
        raw = await session.get_file(f"{_TERMINAL_DIR}/{superstep_id}.json")
    except Exception:
        return None
    try:
        record = _TerminalRecord.model_validate_json(raw)
    except (ValidationError, UnicodeDecodeError):
        return None
    if record.superstep_id != superstep_id:
        return None
    return record


async def persist_terminal_record(
    session: SandboxSession, superstep_id: str, frame: ResultFrame, event: StreamEvent
) -> None:
    """Write the durable terminal record for a resumed super-step BEFORE reporting the terminal.

    Captures the exact output plus the session id + usage for observability.
    """
    record = _TerminalRecord(
        superstep_id=superstep_id,
        session_id=frame.session_id,
        usage=frame.usage,
        structured=isinstance(event, StructuredFinal),
        text=event.text if isinstance(event, MessageFinal) else None,
        data=event.data if isinstance(event, StructuredFinal) else None,
    )
    await session.put_file(f"{_TERMINAL_DIR}/{superstep_id}.json", record.model_dump_json().encode("utf-8"))


def event_from_terminal_record(record: _TerminalRecord) -> StreamEvent:
    """Reconstruct the terminal stream event from a durable terminal record.

    Mirrors :func:`~tai42_agents.claude_code.frames.terminal_event` so a redelivered resume
    re-produces the SAME drained value the original terminal did.
    """
    if record.structured:
        return StructuredFinal(data=record.data)
    return MessageFinal(text=record.text or "")
