"""Session credential resolution, injection, and post-terminal scrubbing for ``claude_code``.

Resolves the session creds into the baked ``spec.env`` plus per-turn bearer credential-helper
files, removes the injected material under ``.claude-home`` on a terminal exit, and redacts
injected-credential values from the kept transcript when the platform flag is on. Imports
settings TYPES only, so importing this module never triggers ``ClaudeCodeSettings`` validation.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.sandbox import SandboxSession

from tai42_agents.claude_code.errors import ClaudeCodeError
from tai42_agents.claude_code.settings import ClaudeCodeSettings, ConnectionCred, StaticCred

# Short exec ceiling for the volume-authoring / scrub commands (reset, payload write, cred
# scrub) — distinct from the turn-scoped ``run_timeout_seconds`` the drive itself runs under.
_SHORT_EXEC_TIMEOUT: Final[float] = 60.0

# Workspace-relative path (rooted at ``session.workspace_path``) holding the per-turn bearer
# credential-helper files.
_CREDS_DIR = ".claude-home/.creds"


class _BearerMaterial(BaseModel):
    """A refreshable connection cred to re-materialize as a credential-helper file each turn."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    env_name: str
    token: SecretStr | None
    headers: dict[str, SecretStr]


async def resolve_creds(
    settings: ClaudeCodeSettings,
) -> tuple[dict[str, SecretStr], list[str], list[_BearerMaterial]]:
    """Resolve the session creds into ``(spec_env, static_env_names, bearer)``.

    The one model credential + every STATIC ``delivery="env"`` value ride ``spec.env`` (baked
    at create); every refreshable ``delivery="bearer"`` cred is materialized per-turn as a
    credential-helper file. A ``required`` connection resolving to nothing raises loudly.
    """
    model_env_name, model_secret = settings.model_credential()
    spec_env: dict[str, SecretStr] = {model_env_name: model_secret}
    static_env_names: list[str] = []
    bearer: list[_BearerMaterial] = []
    for cred in settings.creds:
        if isinstance(cred, StaticCred):
            spec_env[cred.env_name] = cred.value
            static_env_names.append(cred.env_name)
            continue
        if not (isinstance(cred, ConnectionCred)):
            raise AssertionError  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
        resolved = await tai42_app.connectors.resolve_connection_auth(
            cred.connection_id, cred.provider_id, cred.sub_service
        )
        _inject_connection_cred(cred, resolved, spec_env, static_env_names, bearer)
    return spec_env, static_env_names, bearer


def _inject_connection_cred(
    cred: ConnectionCred,
    resolved: ResolvedConnectionAuth | None,
    spec_env: dict[str, SecretStr],
    static_env_names: list[str],
    bearer: list[_BearerMaterial],
) -> None:
    if resolved is None or (resolved.access_token is None and not resolved.env and not resolved.headers):
        if cred.required:
            raise ClaudeCodeError(
                f"required connection cred {cred.env_name!r} resolved to nothing for the current caller"
            )
        return
    # Static transport-partitioned env is baked into spec.env regardless of delivery.
    for key, value in resolved.env.items():
        spec_env[key] = value
        static_env_names.append(key)
    if cred.delivery == "env":
        if resolved.access_token is not None:
            spec_env[cred.env_name] = resolved.access_token
            static_env_names.append(cred.env_name)
        return
    # delivery == "bearer": refreshable material re-written per turn as a helper file.
    bearer.append(_BearerMaterial(env_name=cred.env_name, token=resolved.access_token, headers=resolved.headers))


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


def _secret_values(spec_env: dict[str, SecretStr], bearer: list[_BearerMaterial]) -> list[str]:
    """Every injected secret STRING value, for transcript redaction.

    The baked ``spec.env`` values plus each bearer token/header value.
    """
    values = [v.get_secret_value() for v in spec_env.values()]
    for material in bearer:
        if material.token is not None:
            values.append(material.token.get_secret_value())
        values.extend(v.get_secret_value() for v in material.headers.values())
    return [v for v in values if v]


def _bearer_file(material: _BearerMaterial) -> str:
    lines: list[str] = []
    if material.token is not None:
        lines.append(f"Authorization: Bearer {material.token.get_secret_value()}")
    for key, value in material.headers.items():
        lines.append(f"{key}: {value.get_secret_value()}")
    return "\n".join(lines) + "\n"
