"""Per-turn hermetic workspace authoring for ``claude_code``: reset the adapter-owned config
and payload trees, write ``.claude/settings.json``, re-materialize the bearer files, sync the
skills, ship the runner payload, and build the runner options payload."""

from __future__ import annotations

import json
from typing import Any

from tai42_contract.sandbox import SandboxSession

from tai42_agents.claude_code.credentials import _CREDS_DIR, _SHORT_EXEC_TIMEOUT, _bearer_file, _BearerMaterial
from tai42_agents.claude_code.errors import ClaudeCodeError
from tai42_agents.claude_code.options import build_options_payload, credential_env_names
from tai42_agents.claude_code.payload import runner_payload_files
from tai42_agents.claude_code.settings import ClaudeCodeSettings
from tai42_agents.claude_code.skills_sync import sync_skills

# Workspace-relative paths inside the session volume (rooted at ``session.workspace_path``).
_CLAUDE_CONFIG_DIR = "project/.claude"
_RUNNER_PAYLOAD_DIR = ".runner/payload"


async def materialize(
    session: SandboxSession,
    *,
    ws: str,
    settings: ClaudeCodeSettings,
    bearer: list[_BearerMaterial],
    options_snapshot: dict[str, Any],
) -> None:
    """Author the hermetic workspace for one turn (idempotent, re-run every turn)."""
    # RESET the adapter-owned config + payload trees so no agent-written file survives.
    await _exec_ok(session, ["rm", "-rf", f"{ws}/{_CLAUDE_CONFIG_DIR}", f"{ws}/{_RUNNER_PAYLOAD_DIR}"])
    await session.put_file(
        f"{_CLAUDE_CONFIG_DIR}/settings.json",
        json.dumps(_settings_json(settings), indent=2).encode("utf-8"),
    )
    # RE-WRITE the bearer credential-helper files EVERY TURN from the fresh resolution.
    for material in bearer:
        await session.put_file(f"{_CREDS_DIR}/{material.env_name}", _bearer_file(material).encode("utf-8"))
    await sync_skills(
        session,
        skill_names=options_snapshot["skills"],
        inline_skills=options_snapshot["inline_skills"],
    )
    for name, content in runner_payload_files():
        await session.put_file(f"{_RUNNER_PAYLOAD_DIR}/{name}", content)


def _settings_json(settings: ClaudeCodeSettings) -> dict[str, Any]:
    """The adapter-authored ``.claude/settings.json``: the permission floor, telemetry off,
    and the operator's ``hook_settings`` fragment (verbatim)."""
    doc: dict[str, Any] = {
        "permissions": {"defaultMode": "acceptEdits"},
        "env": {"DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1"},
    }
    if settings.hook_settings is not None:
        doc["hooks"] = settings.hook_settings
    return doc


def build_payload(
    *,
    settings: ClaudeCodeSettings,
    ws: str,
    options_snapshot: dict[str, Any],
    static_env_names: list[str],
    model_env_name: str,
    resume_id: str | None,
) -> dict[str, Any]:
    return build_options_payload(
        ws=ws,
        system_prompt=options_snapshot["system_message"],
        tool_names=options_snapshot["tool_names"],
        skills=options_snapshot["skills"],
        subagents=options_snapshot["subagents"],
        response_format=options_snapshot["response_format"],
        max_turns=options_snapshot["max_turns"] or settings.max_turns,
        max_budget_usd=settings.max_budget_usd,
        model=settings.model,
        secret_env_names=credential_env_names(model_env_name, static_env_names),
        session_id=None,
        resume=resume_id,
    )


async def _exec_ok(session: SandboxSession, argv: list[str]) -> None:
    result = await session.exec(argv, timeout_seconds=_SHORT_EXEC_TIMEOUT)
    if result.exit_code != 0:
        raise ClaudeCodeError(f"workspace command {argv!r} failed ({result.exit_code}): {result.stderr}")
