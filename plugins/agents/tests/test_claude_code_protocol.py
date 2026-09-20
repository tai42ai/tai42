"""``claude_code`` JSONL protocol: framing, the version gate, and unknown-frame loudness."""

from __future__ import annotations

import json

import pytest

from tai42_agents.claude_code.protocol import (
    PROTOCOL_VERSION,
    AskFrame,
    HelloFrame,
    ProtocolError,
    ResultFrame,
    StartFrame,
    ToolCallFrame,
    dump_frame,
    parse_up_frame,
)


def test_dump_frame_is_one_newline_terminated_line() -> None:
    raw = dump_frame(StartFrame(options={}, prompt={"text": "hi"}))
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    decoded = json.loads(raw)
    assert decoded["v"] == PROTOCOL_VERSION
    assert decoded["type"] == "start"


def test_parse_hello_and_result() -> None:
    hello = parse_up_frame(json.dumps({"v": 1, "type": "hello", "sdk_version": "0.1.0", "session_id": "s"}))
    assert isinstance(hello, HelloFrame)
    assert hello.session_id == "s"
    result = parse_up_frame(json.dumps({"v": 1, "type": "result", "terminal_reason": "completed", "result": "done"}))
    assert isinstance(result, ResultFrame)
    assert result.terminal_reason == "completed"


def test_parse_ask_defaults_to_sync() -> None:
    ask = parse_up_frame(json.dumps({"v": 1, "type": "ask", "ask_id": "a", "question": "q"}))
    assert isinstance(ask, AskFrame)
    assert ask.mode == "sync"


def test_parse_tool_call() -> None:
    frame = parse_up_frame(
        json.dumps({"v": 1, "type": "tool_call", "call_id": "c", "tool_name": "t", "arguments": {"a": 1}})
    )
    assert isinstance(frame, ToolCallFrame)
    assert frame.tool_name == "t"


def test_version_mismatch_raises() -> None:
    with pytest.raises(ProtocolError, match="protocol version"):
        parse_up_frame(json.dumps({"v": 2, "type": "hello", "sdk_version": "0.1.0", "session_id": "s"}))


def test_unknown_type_raises() -> None:
    with pytest.raises(ProtocolError, match="unknown or malformed"):
        parse_up_frame(json.dumps({"v": 1, "type": "mystery"}))


def test_non_json_line_raises() -> None:
    with pytest.raises(ProtocolError, match="non-JSON"):
        parse_up_frame("{not json")


def test_non_object_raises() -> None:
    with pytest.raises(ProtocolError, match="not a JSON object"):
        parse_up_frame("[1, 2, 3]")
