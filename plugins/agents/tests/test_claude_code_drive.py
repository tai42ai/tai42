"""``claude_code`` end-to-end drives over the fake sandbox + a scripted runner stub.

These are tool-face (ephemeral, thread-less) runs: no workspace lease, no Redis. The fake
sandbox runs ``python -m tai_runner`` as a real subprocess, so the whole exec path — framing,
stdin answers, session-id capture off the init frame, usage emission — is exercised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.agent.events import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    StructuredFinal,
    SuspendedFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.app import tai42_app
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.monitoring.models import SpanKind
from tests._claude_app import LocalApp, RecordingWriter, build_local_app
from tests._claude_stubs import (
    ASYNC_ASK,
    ENV_CRED_ECHO,
    EVENTS_RICH,
    FATAL,
    MESSAGE,
    STRUCTURED,
    SYNC_ASK,
    TOOL_CALL,
    TOOL_CALL_UNGRANTED,
    VERSION_MISMATCH,
    payload_for,
)

import tai42_agents.claude_code.agent as agent_module
from tai42_agents.claude_code.agent import ClaudeCodeAgent, ClaudeCodeError
from tai42_agents.claude_code.protocol import ProtocolError
from tai42_agents.claude_code.settings import ClaudeCodeSettings, ConnectionCred, StaticCred

_DIGEST = "registry.example/claude@sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _clear_live_sessions() -> Iterator[None]:
    agent_module._LIVE_SESSIONS.clear()
    yield
    agent_module._LIVE_SESSIONS.clear()


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> ClaudeCodeSettings:
    base: dict[str, Any] = {"session_image": _DIGEST, "api_key": SecretStr("modelkey")}
    base.update(overrides)
    settings = ClaudeCodeSettings(**base)
    monkeypatch.setattr(agent_module, "claude_code_settings", lambda: settings)
    return settings


def _run(app: LocalApp, **kwargs: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        with tai42_app.bound(app):
            return [event async for event in ClaudeCodeAgent().astream(**kwargs)]

    return asyncio.run(_collect())


def test_message_drive_yields_delta_and_final(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    events = _run(build_local_app(), user_message="hi")
    assert any(isinstance(e, MessageDelta) and e.text == "hello world" for e in events)
    finals = [e for e in events if isinstance(e, MessageFinal)]
    assert len(finals) == 1
    assert finals[0].text == "hello world"


def test_structured_drive_yields_structured_final(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(STRUCTURED))
    events = _run(build_local_app(), user_message="hi", response_format={"title": "Ans", "type": "object"})
    finals = [e for e in events if isinstance(e, StructuredFinal)]
    assert len(finals) == 1
    assert finals[0].data == {"answer": 42}


def test_sync_ask_is_answered_adapter_side(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(SYNC_ASK))
    asked: list[str] = []

    async def ask_user(question: str, **_: Any) -> Any:
        asked.append(question)
        return "blue"

    events = _run(build_local_app(ask_user=ask_user), user_message="hi")
    assert asked == ["color?"]
    assert any(isinstance(e, MessageFinal) and "answer=blue" in e.text for e in events)


def test_proxied_tool_call_runs_under_run_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(TOOL_CALL))
    app = build_local_app(tool_runners={"mytool": lambda **kw: {"echo": kw}})
    token = set_request_user_id("user-1")
    try:
        events = _run(app, user_message="hi", tool_names=["mytool"])
    finally:
        reset_request_user_id(token)
    assert any(isinstance(e, MessageDelta) and "err=False" in e.text for e in events)


def test_tool_call_outside_allowlist_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(TOOL_CALL_UNGRANTED))
    app = build_local_app(tool_runners={"granted": lambda **kw: 1})
    token = set_request_user_id("user-1")
    try:
        with pytest.raises(ProtocolError, match="outside the granted allowlist"):
            _run(app, user_message="hi", tool_names=["granted"])
    finally:
        reset_request_user_id(token)


def test_usage_emits_into_active_trace_via_span_update(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, model="claude-x")
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    writer = RecordingWriter()
    writer.trace_id = "trace-1"
    _run(build_local_app(writer=writer), user_message="hi")
    assert len(writer.spans) == 1
    span = writer.spans[0]
    assert span["kind"] == SpanKind.LLM
    assert span["model"] == "claude-x"
    assert span["usage_details"] == {"input_tokens": 3, "output_tokens": 5}
    # The cost half must NOT route through update_current_span (which carries no cost).
    assert writer.update_current_calls == []


def test_usage_not_emitted_without_active_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    writer = RecordingWriter()  # trace_id stays None
    _run(build_local_app(writer=writer), user_message="hi")
    assert writer.spans == []


def test_async_ask_on_ephemeral_run_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(ASYNC_ASK))
    events = _run(build_local_app(), user_message="hi")
    # No park on a thread-less run; the model got a tool error instead.
    assert not any(isinstance(e, SuspendedFinal) for e in events)
    assert any(isinstance(e, MessageDelta) and "refused=True" in e.text for e in events)


def test_ephemeral_run_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    _run(build_local_app(), user_message="hi")
    assert agent_module._LIVE_SESSIONS == {}


def test_sdk_version_mismatch_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(VERSION_MISMATCH))
    with pytest.raises(ProtocolError, match="version"):
        _run(build_local_app(), user_message="hi")


def test_tool_names_without_identity_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    with pytest.raises(ClaudeCodeError, match="no bound execution identity"):
        _run(build_local_app(), user_message="hi", tool_names=["t"])


def test_fatal_frame_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner ``fatal`` up-frame is a loud protocol error, never a silent terminal."""
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(FATAL))
    with pytest.raises(ProtocolError, match="fatal error"):
        _run(build_local_app(), user_message="hi")


def test_proxied_tool_exception_round_trips_as_error_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxied tool that RAISES is returned to the runner as an ``is_error`` result frame
    carrying the exception text — never swallowed, never crashing the drive."""
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(TOOL_CALL))

    def boom(**_kw: Any) -> Any:
        raise RuntimeError("tool boom")

    app = build_local_app(tool_runners={"mytool": boom})
    token = set_request_user_id("user-1")
    try:
        events = _run(app, user_message="hi", tool_names=["mytool"])
    finally:
        reset_request_user_id(token)
    # The stub echoed the result frame back: the error flag is set and the exception text carried.
    assert any(isinstance(e, MessageDelta) and "err=True" in e.text and "tool boom" in e.text for e in events)


def test_non_text_events_map_to_contract_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's ``thinking`` / ``tool_use`` / ``tool_result`` events map to
    ReasoningStep / ToolCallStep / ToolResultStep; a blank ``thinking`` is dropped."""
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(EVENTS_RICH))
    events = _run(build_local_app(), user_message="hi")
    reasoning = [e for e in events if isinstance(e, ReasoningStep)]
    assert [e.text for e in reasoning] == ["pondering"]  # the whitespace-only thinking is skipped
    calls = [e for e in events if isinstance(e, ToolCallStep)]
    assert len(calls) == 1
    assert calls[0].tool == "grep"
    assert calls[0].args == {"q": "x"}
    assert calls[0].call_id == "u1"
    results = [e for e in events if isinstance(e, ToolResultStep)]
    assert len(results) == 1
    assert results[0].call_id == "u1"
    assert results[0].result == "hit"
    assert results[0].is_error is False


def test_static_and_env_connection_creds_reach_the_clean_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """§A5 credential passthrough: a STATIC cred bakes its value under ``env_name`` in the CLEAN
    session env, and a connection cred with ``delivery="env"`` bakes its RESOLVED value under
    ``env_name`` too — both distinct from the per-turn bearer FILE path. A regression here would
    silently drop an operator's service cred, so assert both land in the runner's ``os.environ``."""
    _settings(
        monkeypatch,
        creds=[
            StaticCred(env_name="SERVICE_TOKEN", value=SecretStr("static-secret")),
            ConnectionCred(
                env_name="CONN_KEY",
                connection_id="conn-1",
                provider_id="svc",
                sub_service="api",
                delivery="env",
            ),
        ],
    )
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(ENV_CRED_ECHO))
    app = build_local_app(resolver=lambda *_a: ResolvedConnectionAuth(access_token=SecretStr("resolved-env-val")))
    events = _run(app, user_message="hi")
    finals = [e for e in events if isinstance(e, MessageFinal)]
    assert len(finals) == 1
    assert finals[0].text == "static=static-secret,conn=resolved-env-val"
