"""Operator settings for the durable ``langchain_deep_agent`` session model.

The ``StateBackend``→``SandboxSessionBackend`` swap (§B2) gives the deep agent a live
sandbox session per threaded run, so it needs its own operator group: the session image,
the connection-reference SERVICE creds injected into that session's shell, the network
posture, the resource caps, and the crash-resume declaration.

PLANNER PICK (flagged, not a ruling): ``env_prefix="TAI_AGENTS_LANGCHAIN_DEEP_"`` — one
coherent ``TAI_AGENTS_`` family, a distinct group per agent so a deep-agent image is never
named under another agent's prefix.

The MODEL credential is deliberately ABSENT: the deep agent's LLM call runs SERVER-side via
``get_llm_async`` (already metered by the monitoring path), so ONLY service creds enter its
sandbox — the sole cred asymmetry with a coding agent whose model call runs in-session.
"""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.sandbox import SandboxNetwork
from tai42_kit.settings import TaiBaseSettings, settings_cache

from tai42_agents._internal.session_cred import ConnectionCred, SessionCredSpec, StaticCred

# Re-exported so importers of the deep-agent settings surface see the canonical shared cred
# model (defined once in ``_internal.session_cred``, parsed identically by both agents).
__all__ = [
    "ConnectionCred",
    "LangchainDeepAgentSettings",
    "SessionCredSpec",
    "StaticCred",
    "langchain_deep_agent_crash_resume",
    "langchain_deep_agent_settings",
]

# A digest reference pins the exact image bytes: ``<name>@sha256:<64 hex>``. A bare tag is
# mutable, so it is rejected loudly at run start — the published lean exec image is signed
# and referenced by digest.
_DIGEST_REFERENCE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


class LangchainDeepAgentSettings(TaiBaseSettings):
    """The durable-session operator group for ``langchain_deep_agent``.

    Self-registers with the settings registry (like ``AgentsLimitsSettings``), so a
    live-reload reset re-reads the env with no extra wiring.
    """

    model_config = SettingsConfigDict(env_prefix="TAI_AGENTS_LANGCHAIN_DEEP_")

    # REQUIRED, digest-pinned. Points at the lean python3 + coreutils exec session image
    # — NOT a coding-agent image; the deep agent's scratch backend needs only
    # python3/coreutils for BaseSandbox's shell-derived file ops and its built-in shell. The
    # empty default is not a usable value: the digest validator below rejects it LOUDLY at run
    # start, so an unconfigured deployment fails rather than silently running an unpinned image.
    session_image: str = ""

    # The operator's session-cred list (§A5/§B4). Each entry resolves per-caller; the deep
    # agent's sandbox shell gets ONLY these service creds (never a model credential).
    creds: list[SessionCredSpec] = Field(default_factory=list)

    # The workspace/session idle-reap horizon; also feeds the workspace retention horizon that
    # bounds a park (retention = min(checkpoint, workspace), §B3.1). Must be positive.
    session_ttl_seconds: int = Field(default=86400, gt=0)

    # NARROWS the platform egress posture (default OPEN). ``None`` inherits the platform
    # posture; a set value must be at or tighter than it, enforced at the kit create
    # chokepoint (a looser value is a loud error there, never a silent widen).
    network: SandboxNetwork | None = None

    # The spec resource caps, provider-enforced or rejected. ``None`` leaves them unset.
    cpu: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)

    # PARITY with the coding agent: the per-agent re-dispatch declaration, DECLARED
    # to the skeleton at registration as ``meta={"tai42/crash_resume": <setting>}``. RECYCLE-class
    # so a hot change re-registers and re-declares the meta (it is captured once at registration,
    # so a hot change would otherwise leave the registered meta stale — the same staleness argument
    # as the sandbox knobs).
    crash_resume: bool = Field(default=False, json_schema_extra={"reload": "recycle"})

    @model_validator(mode="after")
    def _check_digest_reference(self) -> LangchainDeepAgentSettings:
        if not _DIGEST_REFERENCE_RE.match(self.session_image):
            raise ValueError(
                f"session_image must be a digest reference '<name>@sha256:<64 hex>', not a mutable tag: "
                f"{self.session_image!r}"
            )
        return self


class _CrashResumeMeta(TaiBaseSettings):
    """A lightweight read of ONLY ``TAI_AGENTS_LANGCHAIN_DEEP_CRASH_RESUME`` for the
    registration-time meta declaration, requiring NONE of the full model's creds/digest image.

    Importing ``langchain_deep_agent.agent`` must NOT trigger the full
    :class:`LangchainDeepAgentSettings` validation: the digest-pinned ``session_image`` config
    error is declared to fire at RUN START, before any sandbox session is acquired — never at
    plugin import (so a marketplace/plugin-spec or import-graph read that merely imports the module
    never needs the operator env). Reading the full settings here would REGRESS a previously
    zero-config agent to raising on bare import. The ``crash_resume`` registration meta is
    recycle-class and read ONCE at registration, so it is sourced HERE from a model that shares the
    ``TAI_AGENTS_LANGCHAIN_DEEP_`` prefix (``TAI_AGENTS_LANGCHAIN_DEEP_CRASH_RESUME`` still controls
    it) but declares only this one defaulted field. ``registry_exclude`` keeps this internal reader
    out of the settings registry — the canonical :class:`LangchainDeepAgentSettings` remains the
    declared group that owns ``crash_resume`` and its recycle disposition.
    """

    registry_exclude: ClassVar[bool] = True

    model_config = SettingsConfigDict(env_prefix="TAI_AGENTS_LANGCHAIN_DEEP_")

    crash_resume: bool = Field(default=False, json_schema_extra={"reload": "recycle"})


def langchain_deep_agent_crash_resume() -> bool:
    """Read the ``crash_resume`` recycle-class setting for the registration meta WITHOUT the full
    :class:`LangchainDeepAgentSettings` validation — importing the agent module must not require the
    digest-pinned ``session_image`` or any other operator env."""
    return _CrashResumeMeta().crash_resume


@settings_cache
def langchain_deep_agent_settings() -> LangchainDeepAgentSettings:
    return LangchainDeepAgentSettings()
