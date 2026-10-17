"""The per-worker cache of live ``claude_code`` sandbox sessions for threaded workspaces."""

from __future__ import annotations

import logging

from tai42_contract.sandbox import Sandbox, SandboxSession, SandboxSessionNotFoundError, SandboxSessionSpec

logger = logging.getLogger(__name__)

# Per-worker live-session cache for DETERMINISTIC (threaded) workspaces, so a reused session
# keeps its create-time ``spec.env`` (the refresh channel is the per-turn bearer file, not a
# re-baked env). Ephemeral ``uuid4`` workspaces are never cached. The cross-worker mutex is the
# Redis workspace lease; this cache only avoids re-creating within one worker.
LIVE_SESSIONS: dict[str, SandboxSession] = {}


async def live_session(sandbox: Sandbox, spec: SandboxSessionSpec, workspace_key: str) -> SandboxSession:
    """The worker's cached session for ``workspace_key``, kept alive, or a new one over the same workspace.

    The sandbox ends a session this cache still holds — a reap of a session idle past its ttl (a park waiting on
    its answer) keeps the persistent workspace, an explicit destroy removes it. The gone entry is dropped and
    logged, and a new session opens over the workspace; a resume over a removed workspace then finds no SDK
    session id and fails loudly in the drive.
    """
    cached = LIVE_SESSIONS.get(workspace_key)
    if cached is not None:
        try:
            await cached.touch()
        except SandboxSessionNotFoundError:
            logger.info(
                f"claude_code session {cached.id} for workspace {workspace_key} is gone from the sandbox; opening a "
                "new one over the workspace"
            )
            del LIVE_SESSIONS[workspace_key]
        else:
            return cached
    session = await sandbox.create_session(spec)
    LIVE_SESSIONS[workspace_key] = session
    return session
