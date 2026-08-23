"""Build the ``ClaudeAgentOptions`` PAYLOAD — a plain JSON-able dict the in-session runner
turns into the real SDK object.

The adapter never imports the SDK, so it ships the runner a description of the options and the
runner constructs ``ClaudeAgentOptions`` from it. Two security invariants are baked in here:

* **Default-deny tools** — the payload names the allowed WRITE root (``{ws}/project``) and the
  excluded subtree (``{ws}/project/.claude``); the runner enforces a ``can_use_tool``
  allowlist, NEVER ``allowed_tools`` for a path-checked tool (which ``allowed_tools`` would
  bypass). An adapter-written file can never configure the next turn.
* **Fixed env allowlist** — the host env is NEVER spread. ``env`` carries only the non-secret
  fixed vars (HOME / ``CLAUDE_CONFIG_DIR`` / XDG targets / telemetry-off); the secret
  credential VALUES ride the session ``spec.env`` the provider injects on every exec, and
  ``env_passthrough`` names the secret vars the runner copies from its OWN process env into the
  spawned claude's env — so no secret value travels in the start frame's options.
"""

from __future__ import annotations

from typing import Any

from tai42_agents.claude_code.settings import ANTHROPIC_API_KEY_ENV, CLAUDE_CODE_OAUTH_TOKEN_ENV

# The pinned-SDK ``permission_mode`` literal for auto-approve: the sandbox IS the boundary, so
# the SDK never prompts. The real gate is the runner's default-deny ``can_use_tool`` allowlist.
PERMISSION_MODE = "bypassPermissions"

# Telemetry/updater kill switches every session pins.
_TELEMETRY_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
}


def build_home_env(ws: str) -> dict[str, str]:
    """The HOME / ``CLAUDE_CONFIG_DIR`` / XDG targets, all pinned at ``{ws}/.claude-home`` so no
    host-user directory is ever read or written."""
    home = f"{ws}/.claude-home"
    return {
        "HOME": home,
        "CLAUDE_CONFIG_DIR": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_STATE_HOME": f"{home}/.local/state",
    }


def build_env_allowlist(ws: str) -> dict[str, str]:
    """The FIXED non-secret env the spawned claude runs under: telemetry-off + the pinned
    HOME/XDG targets. The host env is never spread; secret values ride ``spec.env`` and are
    named in ``env_passthrough`` (see :func:`build_options_payload`)."""
    return {**_TELEMETRY_ENV, **build_home_env(ws)}


def build_agent_definitions(subagents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map the caller's ``subagents`` specs to the SDK ``AgentDefinition`` shape, keyed by name.

    Each caller name is validated to the workspace-key charset at the door (in the agent), so a
    subagent name is safe to key on here.
    """
    definitions: dict[str, dict[str, Any]] = {}
    for spec in subagents:
        name = spec["name"]
        definitions[name] = {
            "description": spec.get("description", ""),
            "prompt": spec.get("system_prompt", spec.get("prompt", "")),
            "tools": list(spec.get("tool_names", [])),
        }
    return definitions


def build_options_payload(
    *,
    ws: str,
    system_prompt: str,
    tool_names: list[str],
    skills: list[str],
    subagents: list[dict[str, Any]],
    response_format: dict[str, Any] | None,
    max_turns: int,
    max_budget_usd: float | None,
    model: str | None,
    secret_env_names: list[str],
    session_id: str | None,
    resume: str | None,
) -> dict[str, Any]:
    """Assemble the options payload for one turn (see the module docstring for the invariants).

    ``session_id`` is set on the FIRST turn of a fresh SDK session (the runner reports the
    effective id back on ``hello``); ``resume`` carries a persisted id on a later turn. They are
    mutually exclusive — the caller passes at most one.

    ``secret_env_names`` are the credential env-var NAMES the runner copies from its own process
    env (populated from ``spec.env``) into the spawned claude's env — the values never ride this
    payload.
    """
    payload: dict[str, Any] = {
        "cwd": f"{ws}/project",
        "setting_sources": ["project"],
        "system_prompt": system_prompt,
        "permission_mode": PERMISSION_MODE,
        # The default-deny allowlist bounds: file tools may write under ``project`` EXCLUDING
        # its ``.claude`` config subtree; Bash is allowed (the sandbox is the boundary).
        "allow_write_root": f"{ws}/project",
        "deny_write_subpaths": [f"{ws}/project/.claude"],
        # One in-process SDK MCP server exposes ask_user + one proxy tool per requested name —
        # no HTTP entry, no credential.
        "proxy_tool_names": list(tool_names),
        "skills": list(skills),
        "agents": build_agent_definitions(subagents),
        "max_turns": max_turns,
        "include_partial_messages": True,
        "env": build_env_allowlist(ws),
        "env_passthrough": list(secret_env_names),
    }
    if response_format is not None:
        payload["output_format"] = {"type": "json_schema", "json_schema": response_format}
    if max_budget_usd is not None:
        payload["max_budget_usd"] = max_budget_usd
    if model is not None:
        payload["model"] = model
    if resume is not None:
        payload["resume"] = resume
    elif session_id is not None:
        payload["session_id"] = session_id
    return payload


def credential_env_names(model_env_name: str, static_env_cred_names: list[str]) -> list[str]:
    """The secret env-var names to pass through: the one model credential plus any STATIC
    ``delivery="env"`` service creds. Sanity-checks the model env name is one of the two auth
    vars, so a typo never silently omits the credential."""
    assert model_env_name in {ANTHROPIC_API_KEY_ENV, CLAUDE_CODE_OAUTH_TOKEN_ENV}
    return [model_env_name, *static_env_cred_names]
