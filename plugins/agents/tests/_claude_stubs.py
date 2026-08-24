"""Scripted runner stubs for the ``claude_code`` drive tests.

Each stub is plain Python (NO real ``claude_agent_sdk``) that speaks the versioned JSONL
protocol over stdin/stdout, injected in place of the shipped runner payload by monkeypatching
``tai42_agents.claude_code.agent.runner_payload_files``. Run as ``python -m tai_runner`` inside
the fake sandbox subprocess, so the full exec path (framing, stdin answers, session-id capture)
is exercised end-to-end.
"""

from __future__ import annotations

_PREAMBLE = """import sys, json
def emit(o):
    sys.stdout.write(json.dumps({"v": 1, **o}) + "\\n")
    sys.stdout.flush()
def readline():
    return json.loads(sys.stdin.readline())
start = readline()
options = start["options"]
"""

# A plain text answer: hello (init) + one text event + a completed terminal.
MESSAGE = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "event", "event": {"kind": "text", "text": "hello world"}})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": {"input_tokens": 3, "output_tokens": 5}, "result": "hello world", "is_structured": False})
"""
)

# A structured terminal.
STRUCTURED = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": {"answer": 42}, "is_structured": True})
"""
)

# A sync ask: emit ask, block for the answer, echo it back as text, terminate.
SYNC_ASK = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "ask", "ask_id": "a1", "question": "color?", "mode": "sync"})
ans = readline()
emit({"type": "event", "event": {"kind": "text", "text": "answer=" + str(ans.get("answer"))}})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": "answer=" + str(ans.get("answer")), "is_structured": False})
"""
)

# A proxied tool call: emit tool_call, block for tool_result, echo it back, terminate.
TOOL_CALL = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "tool_call", "call_id": "c1", "tool_name": options["proxy_tool_names"][0], "arguments": {"x": 1}})
res = readline()
echo = "tool=" + str(res.get("result")) + ",err=" + str(res.get("is_error"))
emit({"type": "event", "event": {"kind": "text", "text": echo}})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": str(res.get("result")), "is_structured": False})
"""
)

# A proxied tool call whose tool async-parks: emit tool_call, then either read a stop (the
# adapter parked the tool -> exit) or a tool_result (non-park -> terminate). Mirrors ASYNC_ASK's
# stop handling, but the park is triggered by the TOOL, not the agent's own ask.
TOOL_CALL_PARK = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "tool_call", "call_id": "c1", "tool_name": options["proxy_tool_names"][0], "arguments": {"x": 1}})
frame = readline()
if frame.get("type") == "stop":
    sys.exit(0)
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1", "result": "x", "is_structured": False})
"""
)

# A tool call naming a tool OUTSIDE the granted allowlist — the adapter must reject it loudly.
TOOL_CALL_UNGRANTED = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "tool_call", "call_id": "c1", "tool_name": "not_granted", "arguments": {}})
res = readline()
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1", "result": "x", "is_structured": False})
"""
)

# An async ask: emit ask(async), then either read a stop (park -> exit) or an answer (refused
# on an ephemeral run -> echo the error flag and terminate).
ASYNC_ASK = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "ask", "ask_id": "a1", "question": "deploy?", "mode": "async"})
frame = readline()
if frame.get("type") == "stop":
    sys.exit(0)
emit({"type": "event", "event": {"kind": "text", "text": "refused=" + str(frame.get("is_error"))}})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": "refused=" + str(frame.get("is_error")), "is_structured": False})
"""
)

# A hello whose reported SDK version mismatches the adapter pin — a loud protocol error.
VERSION_MISMATCH = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "9.9.9", "session_id": "sess-1"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1", "result": "x", "is_structured": False})
"""
)

# A runner reporting a FATAL frame — the adapter must raise a loud protocol error, never a
# silent stop.
FATAL = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "fatal", "message": "runner blew up mid-drive"})
"""
)

# A hello reporting a DIFFERENT session id than the one the thread persisted — the adapter must
# refuse the resume loudly rather than silently drive a mismatched SDK session.
MESSAGE_SESSION_OTHER = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-2"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-2",
      "usage": None, "result": "hi", "is_structured": False})
"""
)

# Emits the non-text SDK event kinds (thinking, tool_use, tool_result) so the adapter's event
# mapping to ReasoningStep / ToolCallStep / ToolResultStep is exercised, then terminates.
EVENTS_RICH = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "event", "event": {"kind": "thinking", "text": "pondering"}})
emit({"type": "event", "event": {"kind": "thinking", "text": "   "}})
emit({"type": "event", "event": {"kind": "tool_use", "name": "grep", "input": {"q": "x"}, "id": "u1"}})
emit({"type": "event", "event": {"kind": "tool_result", "id": "u1", "content": "hit", "is_error": False}})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": "done", "is_structured": False})
"""
)

# Reads two env vars baked into the CLEAN session env (a STATIC cred value and a connection cred
# resolved under ``delivery="env"``) and echoes them in the terminal result — so a test can assert
# both the ``static_env_names`` passthrough and the connection ``delivery="env"`` bake reached the
# session. Distinct from CRED_ECHO (which reads a per-turn bearer FILE).
ENV_CRED_ECHO = (
    _PREAMBLE
    + """import os
emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
body = "static=" + os.environ.get("SERVICE_TOKEN", "MISSING") + ",conn=" + os.environ.get("CONN_KEY", "MISSING")
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": body, "is_structured": False})
"""
)


# Reads the bearer credential-helper file the adapter materialized under HOME/.creds and echoes
# its content back in the terminal result — so a test can assert the token that reached the
# session (and that a turn-2 refresh RE-WROTE it) before the terminal scrub removes it.
CRED_ECHO = """import sys, json, os
def emit(o):
    sys.stdout.write(json.dumps({"v": 1, **o}) + "\\n")
    sys.stdout.flush()
json.loads(sys.stdin.readline())
try:
    body = open(os.environ["HOME"] + "/.creds/GH_TOKEN").read().strip()
except OSError:
    body = "MISSING"
emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": body, "is_structured": False})
"""


# A resume drive that reaches a clean terminal with a fixed result — proves the §A3.8 terminal
# idempotence record is written on a resumed super-step's clean terminal.
RESUME_ONCE = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": {"input_tokens": 1, "output_tokens": 1}, "result": "done-once", "is_structured": False})
"""
)

# A resume stub that WOULD return a DIFFERENT terminal if it ever ran — swapped in for the
# redelivery so a re-drive (instead of the durable-record short-circuit) would be observable.
RESUME_REDRIVE = (
    _PREAMBLE
    + """emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": "re-driven", "is_structured": False})
"""
)

# Writes a transcript file under HOME (``{ws}/.claude-home``) carrying the injected model
# credential value, so a ``scrub_transcript``-ON drive can be asserted to redact it (§A3.9 iii).
REDACT_TRANSCRIPT = """import sys, json, os
def emit(o):
    sys.stdout.write(json.dumps({"v": 1, **o}) + "\\n")
    sys.stdout.flush()
json.loads(sys.stdin.readline())
home = os.environ["HOME"]
os.makedirs(home, exist_ok=True)
token = os.environ.get("ANTHROPIC_API_KEY", "")
open(home + "/transcript.jsonl", "w", encoding="utf-8").write("token=" + token + "\\n")
emit({"type": "hello", "sdk_version": "0.2.144", "session_id": "sess-1"})
emit({"type": "result", "terminal_reason": "completed", "session_id": "sess-1",
      "usage": None, "result": "ok", "is_structured": False})
"""


def payload_for(script: str) -> object:
    """A ``runner_payload_files`` replacement that ships exactly this stub as ``tai_runner.py``."""

    def _files() -> list[tuple[str, bytes]]:
        return [("tai_runner.py", script.encode("utf-8"))]

    return _files
