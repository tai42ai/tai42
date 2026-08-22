"""``claude_code`` options payload: the env allowlist, proxy tools, structured output, session
resume, subagent mapping, and the credential passthrough list."""

from __future__ import annotations

import pytest

from tai42_agents.claude_code.options import (
    PERMISSION_MODE,
    build_env_allowlist,
    build_options_payload,
    credential_env_names,
)
from tai42_agents.claude_code.settings import ANTHROPIC_API_KEY_ENV

_WS = "/workspace"


def _payload(**overrides: object) -> dict:
    base: dict = {
        "ws": _WS,
        "system_prompt": "be helpful",
        "tool_names": [],
        "skills": [],
        "subagents": [],
        "response_format": None,
        "max_turns": 100,
        "max_budget_usd": None,
        "model": None,
        "secret_env_names": [ANTHROPIC_API_KEY_ENV],
        "session_id": None,
        "resume": None,
    }
    base.update(overrides)
    return build_options_payload(**base)


def test_env_allowlist_pins_home_and_xdg_without_host_spread() -> None:
    env = build_env_allowlist(_WS)
    assert env["HOME"] == f"{_WS}/.claude-home"
    assert env["CLAUDE_CONFIG_DIR"] == f"{_WS}/.claude-home"
    assert env["XDG_CONFIG_HOME"].startswith(f"{_WS}/.claude-home")
    assert env["DISABLE_TELEMETRY"] == "1"
    # A canary host var is never spread into the fixed allowlist.
    assert "PATH" not in env
    assert "SSH_AUTH_SOCK" not in env


def test_default_deny_bounds_and_permission_mode() -> None:
    payload = _payload()
    assert payload["cwd"] == f"{_WS}/project"
    assert payload["setting_sources"] == ["project"]
    assert payload["permission_mode"] == PERMISSION_MODE
    assert payload["allow_write_root"] == f"{_WS}/project"
    assert payload["deny_write_subpaths"] == [f"{_WS}/project/.claude"]
    assert payload["include_partial_messages"] is True


def test_secret_values_never_ride_the_payload() -> None:
    payload = _payload(secret_env_names=[ANTHROPIC_API_KEY_ENV, "GH_TOKEN"])
    assert payload["env_passthrough"] == [ANTHROPIC_API_KEY_ENV, "GH_TOKEN"]
    # Only names travel; no value appears anywhere in the payload env.
    assert ANTHROPIC_API_KEY_ENV not in payload["env"]


def test_proxy_tools_map_one_per_name() -> None:
    payload = _payload(tool_names=["alpha", "beta"])
    assert payload["proxy_tool_names"] == ["alpha", "beta"]


def test_response_format_becomes_output_format() -> None:
    schema = {"title": "Ans", "type": "object"}
    payload = _payload(response_format=schema)
    assert payload["output_format"] == {"type": "json_schema", "json_schema": schema}


def test_resume_and_session_id_are_mutually_exclusive() -> None:
    resumed = _payload(resume="sess-9")
    assert resumed["resume"] == "sess-9"
    assert "session_id" not in resumed
    fresh = _payload(session_id="sess-1")
    assert fresh["session_id"] == "sess-1"
    assert "resume" not in fresh


def test_subagents_map_to_agent_definitions() -> None:
    payload = _payload(subagents=[{"name": "reviewer", "description": "d", "system_prompt": "p", "tool_names": ["x"]}])
    assert payload["agents"] == {"reviewer": {"description": "d", "prompt": "p", "tools": ["x"]}}


def test_credential_env_names_prefixes_the_model_var() -> None:
    assert credential_env_names(ANTHROPIC_API_KEY_ENV, ["GH_TOKEN"]) == [ANTHROPIC_API_KEY_ENV, "GH_TOKEN"]


def test_credential_env_names_rejects_a_non_auth_model_var() -> None:
    with pytest.raises(AssertionError):
        credential_env_names("NOT_AN_AUTH_VAR", [])


def test_max_budget_usd_is_included_when_set() -> None:
    assert "max_budget_usd" not in _payload()  # omitted when None
    assert _payload(max_budget_usd=2.5)["max_budget_usd"] == 2.5


def test_runner_payload_loads_the_shipped_template() -> None:
    """The real payload loader ships the packaged runner template as its in-session filename,
    the ``.tmpl`` suffix stripped — the ONLY code that imports the SDK, carried as DATA."""
    from tai42_agents.claude_code.payload import runner_payload_files

    files = dict(runner_payload_files())
    assert "tai_runner.py" in files  # ``tai_runner.py.tmpl`` stripped to its run name
    assert b"claude_agent_sdk" in files["tai_runner.py"]
