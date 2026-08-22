"""A scripted stand-in for the ``claude_code`` in-session runner.

Speaks the v1 JSONL exec protocol (the adapter <-> runner wire) over plain stdio: single-line
JSON frames in, single-line JSON frames out. The provider's runner-selection seam
(``SANDBOX_FAKE_RUNNER=stub:<script>``) routes ``python -m tai_runner`` here instead of the
real materialized payload, so every deterministic ``claude_code`` suite drives the adapter's
full exec path with NO vendor SDK and NO network.

It imports NOTHING vendor — not ``claude_agent_sdk``, not the plugin. The PINNED SDK version
the ``hello`` frame must carry (so the adapter's version gate passes) is read off the
``CLAUDE_AGENT_SDK_VERSION`` env var the provider injects from the plugin's own pin; the
effective ``session_id`` is captured/echoed off the ``start`` frame so a resumed turn reports
the same id the adapter persisted.

The script to replay is named by ``SANDBOX_FAKE_RUNNER_SCRIPT``. Each script exercises one
up-frame path the adapter must handle (a normal/structured answer, a sync/async ask, a proxied
tool call, a fatal, a malformed frame, a stall for the budget door, the session-cred readback).
Missing env or an unknown script name fails LOUDLY on stderr with a nonzero exit — never a
silent partial turn.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from typing import Any

_PROTOCOL_VERSION = 1


class _Runner:
    """One turn's stdio conversation: read down-frames, write up-frames."""

    def __init__(self) -> None:
        self._out = sys.stdout
        self._in = sys.stdin

    def read_frame(self) -> dict[str, Any] | None:
        """The next down-frame (``answer`` / ``tool_result`` / ``stop``), or ``None`` at EOF."""
        line = self._in.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return self.read_frame()
        return json.loads(line)

    def emit(self, frame: dict) -> None:
        """Write one up-frame as a single newline-terminated JSON line, flushed at once."""
        self._out.write(json.dumps({"v": _PROTOCOL_VERSION, **frame}) + "\n")
        self._out.flush()

    def emit_raw(self, line: str) -> None:
        """Write a verbatim line (the malformed-frame path — deliberately not a valid frame)."""
        self._out.write(line + "\n")
        self._out.flush()

    # -- up-frame helpers -------------------------------------------------------------------

    def hello(self, session_id: str) -> None:
        self.emit({"type": "hello", "sdk_version": _sdk_version(), "session_id": session_id})

    def text(self, text: str) -> None:
        self.emit({"type": "event", "event": {"kind": "text", "text": text}})

    def thinking(self, text: str) -> None:
        self.emit({"type": "event", "event": {"kind": "thinking", "text": text}})

    def tool_use(self, *, name: str, call_id: str, arguments: dict) -> None:
        self.emit({"type": "event", "event": {"kind": "tool_use", "name": name, "id": call_id, "input": arguments}})

    def ask(self, *, ask_id: str, question: str, mode: str) -> None:
        self.emit({"type": "ask", "ask_id": ask_id, "question": question, "mode": mode, "answer_format": "text"})

    def tool_call(self, *, call_id: str, tool_name: str, arguments: dict) -> None:
        self.emit({"type": "tool_call", "call_id": call_id, "tool_name": tool_name, "arguments": arguments})

    def result(
        self,
        *,
        session_id: str,
        result: object = None,
        is_structured: bool = False,
        usage: dict | None = None,
        terminal_reason: str = "completed",
    ) -> None:
        self.emit(
            {
                "type": "result",
                "terminal_reason": terminal_reason,
                "session_id": session_id,
                "result": result,
                "is_structured": is_structured,
                "usage": usage,
            }
        )

    def fatal(self, message: str) -> None:
        self.emit({"type": "fatal", "message": message})


def _sdk_version() -> str:
    version = os.environ.get("CLAUDE_AGENT_SDK_VERSION")
    if not version:
        raise RuntimeError(
            "claude_runner_stub requires CLAUDE_AGENT_SDK_VERSION in its env "
            "(the fake provider injects the plugin's pinned version)"
        )
    return version


def _session_id(start: dict[str, Any]) -> str:
    """The effective SDK session id: the resumed id off the start frame, else a fresh one."""
    options = start.get("options") or {}
    resume = options.get("resume")
    return resume if resume else uuid.uuid4().hex


# --- scripts -------------------------------------------------------------------------------
#
# Each script runs one turn after the hello frame is emitted. ``start`` is the parsed start
# frame; ``session_id`` is already resolved (fresh, or the resumed id echoed on the hello).


def _script_answer(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A plain streamed answer: one text delta, then a normal terminal answer."""
    io.text("hello from ")
    io.text("the stub")
    io.result(session_id=session_id, result="hello from the stub")


def _script_structured(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A structured terminal result carrying a top-level ``title`` (the ``response_format`` leg)."""
    io.result(session_id=session_id, result={"title": "stub result", "body": "structured"}, is_structured=True)


def _script_reasoning(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A thinking step then an answer (the ``ReasoningStep`` mapping)."""
    io.thinking("considering the request")
    io.text("done")
    io.result(session_id=session_id, result="done")


def _script_ask_sync(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A SYNC ask: block for the ``answer`` frame the adapter writes, then continue to terminal."""
    io.ask(ask_id="ask-1", question="proceed?", mode="sync")
    frame = io.read_frame()
    if frame is None or frame.get("type") != "answer":
        raise RuntimeError(f"stub expected an answer frame, got {frame!r}")
    io.result(session_id=session_id, result=f"answered:{frame.get('answer')!r}")


def _script_ask_async(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """An ASYNC ask: the adapter parks and interrupts with a ``stop`` frame; emit no terminal."""
    io.ask(ask_id="ask-async", question="proceed later?", mode="async")
    io.read_frame()  # the stop(reason=park) the adapter writes on interrupt


def _script_first_turn_park(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A FIRST-TURN async park: the ask precedes any event/result, the session id already on hello."""
    io.ask(ask_id="ask-first", question="park on turn one?", mode="async")
    io.read_frame()


def _script_tool_call(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """One proxied platform tool call: emit the call, block for its ``tool_result``, then answer."""
    tool_name = _first_tool(start)
    io.tool_call(call_id="call-1", tool_name=tool_name, arguments={"payload": "ping"})
    frame = io.read_frame()
    if frame is None or frame.get("type") != "tool_result":
        raise RuntimeError(f"stub expected a tool_result frame, got {frame!r}")
    io.result(session_id=session_id, result=f"tool:{frame.get('result')!r}")


def _script_two_tool_calls(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """Two proxied tool calls plus a sync ask interleaved; replies routed by call_id / ask_id.

    The single-writer stdio holds each frame atomic; the stub tolerates the replies arriving in
    any order and matches each by its id."""
    tool_name = _first_tool(start)
    io.tool_call(call_id="call-a", tool_name=tool_name, arguments={"payload": "a"})
    io.tool_call(call_id="call-b", tool_name=tool_name, arguments={"payload": "b"})
    io.ask(ask_id="ask-mid", question="continue?", mode="sync")
    pending_calls = {"call-a", "call-b"}
    answered = False
    results: dict[str, object] = {}
    while pending_calls or not answered:
        frame = io.read_frame()
        if frame is None:
            raise RuntimeError("stub stream ended before all replies arrived")
        kind = frame.get("type")
        if kind == "tool_result":
            call_id = str(frame.get("call_id"))
            pending_calls.discard(call_id)
            results[call_id] = frame.get("result")
        elif kind == "answer":
            answered = True
        else:
            raise RuntimeError(f"stub got an unexpected frame {frame!r}")
    io.result(session_id=session_id, result={"results": results}, is_structured=True)


def _script_session_creds(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """Read ``E2E_SVC_TOKEN`` off the clean session env and emit it back so the test asserts the
    connection-reference service cred reached the session env."""
    token = os.environ.get("E2E_SVC_TOKEN", "")
    io.result(session_id=session_id, result=token)


def _script_usage(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A terminal answer carrying SDK-reported usage/cost (the model-cost emission leg)."""
    usage = {"input_tokens": 12, "output_tokens": 34, "total_cost_usd": 0.0042}
    io.result(session_id=session_id, result="counted", usage=usage)


def _script_fatal(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A constant-safe fatal error the adapter must raise on."""
    io.fatal("stub fatal error")


def _script_malformed(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """A malformed frame (wrong protocol version) — the adapter rejects it loudly."""
    io.emit_raw(json.dumps({"v": 999, "type": "result", "terminal_reason": "completed"}))


def _script_stall(io: _Runner, start: dict[str, Any], session_id: str) -> None:
    """Never emit a terminal — block on the idle stdin until the adapter's budget/timeout kills
    the process group (the turn-budget door leg).

    ``read_frame`` blocks on the underlying ``readline``, so this idles with no busy loop and no
    sleep; a ``stop`` interrupt (park/cancel) or EOF (the kill) returns promptly."""
    while True:
        frame = io.read_frame()
        if frame is None or frame.get("type") == "stop":
            return


_SCRIPTS: dict[str, Callable[[_Runner, dict[str, Any], str], None]] = {
    "answer": _script_answer,
    "thread": _script_answer,
    "structured": _script_structured,
    "reasoning": _script_reasoning,
    "ask_sync": _script_ask_sync,
    "ask_async": _script_ask_async,
    "first_turn_park": _script_first_turn_park,
    "tool_call": _script_tool_call,
    "two_tool_calls": _script_two_tool_calls,
    "session_creds": _script_session_creds,
    "usage": _script_usage,
    "fatal": _script_fatal,
    "malformed": _script_malformed,
    "stall": _script_stall,
}


def _first_tool(start: dict[str, Any]) -> str:
    tool_names = start.get("tool_names") or []
    if not tool_names:
        raise RuntimeError("stub tool-call script needs a non-empty tool_names allowlist on the start frame")
    return tool_names[0]


def main() -> int:
    script_name = os.environ.get("SANDBOX_FAKE_RUNNER_SCRIPT")
    if not script_name:
        sys.stderr.write("claude_runner_stub requires SANDBOX_FAKE_RUNNER_SCRIPT in its env\n")
        return 2
    script = _SCRIPTS.get(script_name)
    if script is None:
        sys.stderr.write(f"unknown claude_runner_stub script {script_name!r}; known: {sorted(_SCRIPTS)}\n")
        return 2

    io = _Runner()
    start = io.read_frame()
    if start is None or start.get("type") != "start":
        sys.stderr.write(f"claude_runner_stub expected a start frame, got {start!r}\n")
        return 2

    session_id = _session_id(start)
    io.hello(session_id)
    script(io, start, session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
