"""``TAI_AGENTS_CLAUDE_*`` operator settings for the ``claude_code`` agent.

The concrete ``TaiBaseSettings`` subclass self-registers with the settings registry (like
``AgentsLimitsSettings``), so a live-reload reset re-reads the env with no extra wiring.

Two invariants are enforced HERE, at settings-resolution time — BEFORE any sandbox session is
acquired, so a misconfiguration fails loudly at run start rather than mid-drive:

* **Exactly-one model auth** — ``api_key`` XOR ``oauth_token``. Neither or both is a loud
  config error (no silent precedence). The matching env var (``ANTHROPIC_API_KEY`` XOR
  ``CLAUDE_CODE_OAUTH_TOKEN``) is the ONLY model credential injected into the session.
* **Digest-only session image** — ``session_image`` MUST be a ``...@sha256:<64 hex>`` digest
  reference; a bare tag is rejected (the claude session image is published and pinned by digest).

``SessionCredSpec`` is the operator's session-cred entry (a discriminated union of a plain
static value and a per-caller connection reference); the agent injects ONLY this list plus the
one model credential into a CLEAN session env — never the host env.
"""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.sandbox import SandboxNetwork
from tai42_kit.settings import TaiBaseSettings, settings_cache

from tai42_agents._internal.session_cred import ConnectionCred, SessionCredSpec, StaticCred

# Re-exported so importers of the ``claude_code`` settings surface see the canonical shared
# cred model (defined once in ``_internal.session_cred``, parsed identically by both agents).
__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "CLAUDE_CODE_OAUTH_TOKEN_ENV",
    "ClaudeCodeSettings",
    "ConnectionCred",
    "SessionCredSpec",
    "StaticCred",
    "claude_code_crash_resume",
    "claude_code_settings",
]

# A digest reference pins the exact image bytes: ``<name>@sha256:<64 lowercase hex>``. A bare
# tag (mutable) is refused so a session can never silently run a re-pushed image.
_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

# The env var each auth mode injects into the session — the SDK reads these natively.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
CLAUDE_CODE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


class ClaudeCodeSettings(TaiBaseSettings):
    """Operator settings for the ``claude_code`` agent (``TAI_AGENTS_CLAUDE_*``).

    Part of the plugin's ``TAI_AGENTS_`` family (the park Redis is ``TAI_AGENTS_REDIS_*``).
    """

    model_config = SettingsConfigDict(env_prefix="TAI_AGENTS_CLAUDE_")

    # --- model auth (exactly one; see the after-validator) -----------------------------
    # The metered ``ANTHROPIC_API_KEY`` path.
    api_key: SecretStr | None = None
    # The subscription ``CLAUDE_CODE_OAUTH_TOKEN`` path (from ``claude setup-token``).
    oauth_token: SecretStr | None = None

    # The exact runnable claude session image — a digest reference, never a bare tag.
    session_image: str

    # The operator's session-cred list injected into the CLEAN session env / bearer files.
    creds: list[SessionCredSpec] = Field(default_factory=list)

    # A Claude Code hooks configuration fragment materialized VERBATIM into the
    # adapter-authored ``.claude/settings.json``. EXECUTABLE CONFIGURATION (shell commands run
    # inside sessions) — operator-scoped ONLY, never a caller/``ToolInput`` field.
    hook_settings: dict | None = None

    # Model pin; ``None`` = the SDK default (production deployments should pin).
    model: str | None = None

    # Turn/budget ceilings passed to the runner.
    max_turns: int = Field(default=100, gt=0)
    max_budget_usd: float | None = None

    # The wall-clock ceiling for ONE runner drive and the ``timeout_seconds`` the adapter
    # passes to ``exec_start``. DISTINCT from the kit ``exec_default_timeout_seconds`` (the
    # 300s SHORT-helper default): a coding turn routinely runs minutes. Should be set >= the
    # platform turn budget; if the budget outlives it the drive exec times out first — the turn
    # still ends LOUDLY (``SandboxExecTimeoutError``), never silent.
    run_timeout_seconds: int = Field(default=3600, gt=0)

    # The workspace/session idle-reap deadline (``SandboxSessionSpec.ttl_seconds``).
    session_ttl_seconds: int = Field(default=86400, gt=0)

    # A SETTING that NARROWS the platform egress posture (default OPEN). ``None`` inherits the
    # platform posture; a set value must be at or TIGHTER than it (enforced at the KIT
    # session-create chokepoint, never here — a looser value is a loud error there).
    network: SandboxNetwork | None = None

    # Re-dispatch a recycled DETACHED run at-least-once (re-executes FROM SCRATCH). RECYCLE
    # class: the meta is captured once at registration, so a HOT change would leave it stale. At
    # registration this value is sourced from the lightweight ``_CrashResumeMeta`` (same env var),
    # so declaring the meta never triggers this model's creds/image validation at plugin import.
    crash_resume: bool = Field(default=False, json_schema_extra={"reload": "recycle"})

    @model_validator(mode="after")
    def _validate_auth_and_image(self) -> ClaudeCodeSettings:
        if (self.api_key is None) == (self.oauth_token is None):
            raise ValueError(
                "claude_code requires EXACTLY ONE model credential: set TAI_AGENTS_CLAUDE_API_KEY "
                "(ANTHROPIC_API_KEY) XOR TAI_AGENTS_CLAUDE_OAUTH_TOKEN (CLAUDE_CODE_OAUTH_TOKEN), "
                "never neither and never both."
            )
        if not _DIGEST_RE.fullmatch(self.session_image):
            raise ValueError(
                "claude_code session_image must be a digest reference '<name>@sha256:<64 hex>', "
                f"not a bare tag: {self.session_image!r}"
            )
        return self

    def model_credential(self) -> tuple[str, SecretStr]:
        """The one model credential ``(env_name, secret)`` to inject — the exactly-one auth the
        after-validator already guaranteed is set."""
        if self.api_key is not None:
            return ANTHROPIC_API_KEY_ENV, self.api_key
        assert self.oauth_token is not None  # guaranteed by _validate_auth_and_image
        return CLAUDE_CODE_OAUTH_TOKEN_ENV, self.oauth_token


class _CrashResumeMeta(TaiBaseSettings):
    """A lightweight read of ONLY ``TAI_AGENTS_CLAUDE_CRASH_RESUME`` for the registration-time
    meta declaration, requiring NONE of the full model's creds/image.

    Importing ``claude_code.agent`` must NOT trigger the full ``ClaudeCodeSettings`` validation:
    the exactly-one-auth + digest-image config errors are declared to fire at RUN START, before
    any sandbox session is acquired — never at plugin import (so a marketplace/plugin-spec or
    import-graph read that merely imports the module never needs creds). The ``crash_resume``
    registration meta is recycle-class and read ONCE at registration, so it is sourced HERE from a
    model that shares the ``TAI_AGENTS_CLAUDE_`` prefix (``TAI_AGENTS_CLAUDE_CRASH_RESUME`` still
    controls it) but declares only this one defaulted field. ``registry_exclude`` keeps this
    internal reader out of the settings registry — the canonical ``ClaudeCodeSettings`` remains
    the declared group that owns ``crash_resume`` and its recycle disposition.
    """

    registry_exclude: ClassVar[bool] = True

    model_config = SettingsConfigDict(env_prefix="TAI_AGENTS_CLAUDE_")

    crash_resume: bool = Field(default=False, json_schema_extra={"reload": "recycle"})


def claude_code_crash_resume() -> bool:
    """Read the ``crash_resume`` recycle-class setting for the registration meta WITHOUT the full
    ``ClaudeCodeSettings`` validation — importing the agent module must not require creds/image."""
    return _CrashResumeMeta().crash_resume


@settings_cache
def claude_code_settings() -> ClaudeCodeSettings:
    return ClaudeCodeSettings()  # type: ignore[call-arg]  # required fields come from env
