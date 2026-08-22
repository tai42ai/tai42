"""``claude_code`` contract mechanics: the tool-face schema, the unhonored map, the memory-key
and response-format guards, and the caller-name charset — all raised before any sandbox work."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr, ValidationError

import tai42_agents.claude_code.agent as agent_module
from tai42_agents.claude_code.agent import ClaudeCodeAgent, ClaudeCodeError, ClaudeCodeInput
from tai42_agents.claude_code.settings import ClaudeCodeSettings
from tai42_agents.claude_code.skills_sync import SkillNameError

_DIGEST = "registry.example/claude@sha256:" + "b" * 64


@pytest.fixture
def _settings(monkeypatch: pytest.MonkeyPatch) -> ClaudeCodeSettings:
    settings = ClaudeCodeSettings(session_image=_DIGEST, api_key=SecretStr("k"))  # type: ignore[call-arg]
    monkeypatch.setattr(agent_module, "claude_code_settings", lambda: settings)
    return settings


async def _drain(agent: ClaudeCodeAgent, **kwargs: object) -> None:
    async for _ in agent.astream(**kwargs):  # type: ignore[arg-type]
        pass


def test_thread_id_is_not_a_tool_input_field() -> None:
    assert "thread_id" not in ClaudeCodeInput.model_fields


def test_tool_input_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ClaudeCodeInput(user_message="hi", surprise=1)  # type: ignore[call-arg]


def test_tool_input_defaults() -> None:
    parsed = ClaudeCodeInput(user_message="hi")
    assert parsed.tool_names == []
    assert parsed.skills == []
    assert parsed.subagents == []


def test_registered_name_matches_tool_name() -> None:
    assert ClaudeCodeAgent.tool_name == "claude_code"


@pytest.mark.usefixtures("_settings")
def test_unhonored_param_raises_loudly() -> None:
    with pytest.raises(RuntimeError, match="recursion_limit"):
        asyncio.run(_drain(ClaudeCodeAgent(), user_message="hi", recursion_limit=5))


@pytest.mark.usefixtures("_settings")
def test_live_tools_unhonored() -> None:
    with pytest.raises(RuntimeError, match="tools"):
        asyncio.run(_drain(ClaudeCodeAgent(), user_message="hi", tools=[object()]))


@pytest.mark.usefixtures("_settings")
def test_untitled_response_format_raises() -> None:
    with pytest.raises(ValueError, match="title"):
        asyncio.run(_drain(ClaudeCodeAgent(), user_message="hi", response_format={"type": "object"}))


@pytest.mark.usefixtures("_settings")
def test_blank_thread_id_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(_drain(ClaudeCodeAgent(), user_message="hi", thread_id="  "))


@pytest.mark.usefixtures("_settings")
def test_invalid_skill_name_raises() -> None:
    with pytest.raises(SkillNameError):
        asyncio.run(_drain(ClaudeCodeAgent(), user_message="hi", skills=["../evil"]))


@pytest.mark.usefixtures("_settings")
def test_tool_names_without_identity_is_fail_closed() -> None:
    with pytest.raises(ClaudeCodeError, match="no bound execution identity"):
        asyncio.run(_drain(ClaudeCodeAgent(), user_message="hi", tool_names=["some_tool"]))
