"""Session credential resolution, injection, and post-terminal scrubbing for ``claude_code``.

Resolves the session creds into the baked ``spec.env`` plus per-turn bearer credential-helper
files, removes the injected material under ``.claude-home`` on a terminal exit, and redacts
injected-credential values from the kept transcript when the platform flag is on. Imports
settings TYPES only, so importing this module never triggers ``ClaudeCodeSettings`` validation.
"""

from __future__ import annotations

from typing import Any, Final

from tai42_contract.sandbox import SandboxSession

from tai42_agents._internal.session_cred import SessionCredMaterial, materialize_session_creds
from tai42_agents.claude_code.errors import ClaudeCodeError
from tai42_agents.claude_code.settings import ClaudeCodeSettings

# Short exec ceiling for the volume-authoring / scrub commands (reset, payload write, cred
# scrub) — distinct from the turn-scoped ``run_timeout_seconds`` the drive itself runs under.
_SHORT_EXEC_TIMEOUT: Final[float] = 60.0

# Workspace-relative path (rooted at ``session.workspace_path``) holding the per-turn bearer
# credential-helper files.
_CREDS_DIR = ".claude-home/.creds"


async def resolve_creds(settings: ClaudeCodeSettings) -> SessionCredMaterial:
    """Materialize the session creds by the one session-cred rule, with the model credential on top.

    The model credential and every ``env`` value ride ``spec.env`` (baked at create); every
    bearer cred is re-materialized per turn as a credential-helper file. A cred that cannot be
    materialized raises :class:`~tai42_agents._internal.session_cred.SessionCredError`.
    """
    model_env_name, model_secret = settings.model_credential()
    material = await materialize_session_creds(settings.creds)
    return SessionCredMaterial(
        env={model_env_name: model_secret, **material.env},
        env_names=material.env_names,
        bearer=material.bearer,
    )


async def scrub_credentials(session: SandboxSession, *, ws: str) -> None:
    """Remove injected credential MATERIAL under ``.claude-home`` (TERMINAL exits only).

    An un-removable match is a loud error. The invariant: no injected credential material
    persists after a run reaches a TERMINAL state.
    """
    result = await session.exec(["rm", "-rf", f"{ws}/{_CREDS_DIR}"], timeout_seconds=_SHORT_EXEC_TIMEOUT)
    if result.exit_code != 0:
        raise ClaudeCodeError(f"claude_code credential scrub failed to remove {_CREDS_DIR}: {result.stderr}")


async def redact_transcript(session: SandboxSession, *, ws: str, policy: Any, secrets: list[str]) -> None:
    """Redact injected-credential VALUES from the KEPT session transcript when ``scrub_transcript`` is ON.

    Distinct from the credential-FILE scrub above: this rewrites text, never deletes the
    transcript — resume still reads it. The knob OFF leaves the transcript verbatim (the
    stated env-credential residual stands).
    """
    if not getattr(policy, "scrub_transcript", False) or not secrets:
        return
    script = (
        "import os,sys\n"
        "root=sys.argv[1]\n"
        "marks=sys.argv[2:]\n"
        "for dp,_,fns in os.walk(root):\n"
        "  for fn in fns:\n"
        "    p=os.path.join(dp,fn)\n"
        "    try:\n"
        "      t=open(p,encoding='utf-8').read()\n"
        "    except (OSError,UnicodeDecodeError):\n"
        "      continue\n"
        "    n=t\n"
        "    for m in marks:\n"
        "      n=n.replace(m,'[REDACTED]')\n"
        "    if n!=t:\n"
        "      open(p,'w',encoding='utf-8').write(n)\n"
    )
    result = await session.exec(
        ["python", "-c", script, f"{ws}/.claude-home", *secrets], timeout_seconds=_SHORT_EXEC_TIMEOUT
    )
    if result.exit_code != 0:
        raise ClaudeCodeError(f"claude_code transcript redaction failed: {result.stderr}")
