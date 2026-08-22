"""Durable-session acquisition for a ``langchain_deep_agent`` run.

The ``StateBackend``→``SandboxSessionBackend`` swap (§B2/§B4) gives every run/astream drive a
live sandbox session. This module owns the run-door side of that: acquire the session BEFORE
the graph compiles (the backend needs it), inject the operator's connection-reference SERVICE
creds the SAME way the coding agent does (static ``delivery="env"`` values in the CLEAN
session env at create; refreshable ``delivery="bearer"`` creds as per-turn credential-helper
FILES under ``{ws}/.creds``, an adapter-controlled dir OUTSIDE the agent-writable
``{ws}/project`` tree), serialize threaded turns on the shared cross-worker workspace lease,
compute the workspace retention horizon that bounds a park, and — on a TERMINAL exit — scrub
the bearer credential material (SKIPPED on a park-suspend, whose file stays for the resume).

The MODEL credential is deliberately absent: the deep agent's LLM call runs SERVER-side via
``get_llm_async``, so ONLY service creds enter the session.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import SandboxSession

from tai42_agents._internal.park.lease import LEASE_HEADROOM_SECONDS, workspace_lease
from tai42_agents._internal.sandbox_util import build_policied_spec, workspace_key_for
from tai42_agents.langchain_deep_agent.settings import (
    ConnectionCred,
    LangchainDeepAgentSettings,
    SessionCredSpec,
    StaticCred,
    langchain_deep_agent_settings,
)

#: The registered agent name — the workspace-key namespace and the session label, so a
#: deep-agent volume never collides with another engine's for the same ``thread_id``.
AGENT_NAME: Final[str] = "langchain_deep_agent"

#: The adapter-controlled bearer-credential directory, workspace-relative and OUTSIDE the
#: agent-writable ``project`` subtree, so a credential-helper file is unreachable through the
#: agent's file tools. The deep-agent analogue of the coding agent's ``.claude-home/.creds``.
_CREDS_DIR: Final[str] = ".creds"

#: A generous per-drive wall-clock ceiling used ONLY to size the workspace lease so it never
#: expires under a live drive. The deep-agent turn is bounded by its recursion limit / model
#: calls, not an exec timeout, so this is a lease-sizing constant (not a run timeout): the
#: lease is LONG and never heartbeated (§C4), so it must exceed any real drive.
_DRIVE_CEILING_SECONDS: Final[int] = 3600


def _lease_ms() -> int:
    """The workspace-lease TTL in milliseconds — the drive ceiling plus the volume-cleanup
    headroom (skill copy / cred materialize / teardown scrub), so the lease outlives one whole
    turn including its ``finally``."""
    return (_DRIVE_CEILING_SECONDS + LEASE_HEADROOM_SECONDS) * 1000


@contextlib.asynccontextmanager
async def _optional_workspace_lease(workspace_key: str, *, is_threaded: bool) -> AsyncIterator[None]:
    """Hold the shared cross-worker per-workspace lease for the body — ONLY for a THREADED run (a
    deterministic ``workspace_key`` two workers could both target). A tool-face ``uuid4`` workspace
    no other worker can name takes none, so Redis is not touched. Kept module-level (not an instance
    method) so :meth:`DeepAgentSession.leased` can take the lease BEFORE any session exists — the
    lease must precede session creation, or two same-``thread_id`` workers each open a session on
    the shared volume before either wins the lease (the leak this guards)."""
    if not is_threaded:
        yield
        return
    async with workspace_lease(workspace_key, lease_ms=_lease_ms()):
        yield


class DeepAgentSession:
    """A live sandbox session acquired for one run/astream drive, plus its lease + cred scrub.

    Threaded runs (a ``thread_id``) get a deterministic, agent-namespaced ``workspace_key`` and
    a ``persistent`` durable volume that reattaches across turns; a tool-face run gets a fresh
    ``uuid4`` ``ephemeral`` workspace no other worker can name (so no lease, and it reaps by
    TTL). ``resume`` reacquires by the SAME ``workspace_key`` a threaded first turn used.
    """

    def __init__(
        self,
        *,
        session: SandboxSession,
        settings: LangchainDeepAgentSettings,
        workspace_key: str,
        is_threaded: bool,
        bearer_creds: list[tuple[ConnectionCred, dict[str, SecretStr]]],
    ) -> None:
        self.session = session
        self._settings = settings
        self.workspace_key = workspace_key
        self.is_threaded = is_threaded
        self._bearer_creds = bearer_creds

    @staticmethod
    def _resolve_workspace_key(thread_id: str | None, workspace_key: str | None) -> str:
        """The workspace key this drive targets: the resume-supplied key (reattach the parked
        volume) if given, else the deterministic agent-namespaced key derived from ``thread_id``
        (threaded), else a fresh ``uuid4`` no other worker can name (tool-face). Resolved WITHOUT a
        session so :meth:`leased` can name the lease before one is created."""
        if workspace_key is not None:
            return workspace_key
        if thread_id is not None:
            return workspace_key_for(AGENT_NAME, thread_id)
        return uuid.uuid4().hex

    @classmethod
    @contextlib.asynccontextmanager
    async def leased(
        cls, *, thread_id: str | None, workspace_key: str | None = None
    ) -> AsyncIterator[DeepAgentSession]:
        """Take the shared workspace lease FIRST, then acquire the session + materialize creds
        INSIDE it, yielding the guarded drive.

        On a THREADED (persistent) run the lease is what serializes session creation + credential
        materialization on the shared volume across workers, so it MUST precede
        :meth:`acquire`: were the session created first (as a bare ``acquire`` then ``lease`` would),
        two same-``thread_id`` workers would each open a session and write ``.creds`` before either
        won the lease — the lease-LOSER's session would then LEAK (only TTL-reaped) and its cred
        write could clobber the winner's. Taking the lease first makes the loser raise
        :class:`~tai42_agents._internal.park.errors.WorkspaceLeaseHeldError` from the lease
        ``__aenter__`` BEFORE any session or cred exists. A tool-face (ephemeral) run takes no lease
        and always proceeds. The credential scrub stays the caller's terminal-exit concern (skipped
        on a park-suspend), run in the caller's ``finally`` inside this region."""
        is_threaded = thread_id is not None
        workspace_key = cls._resolve_workspace_key(thread_id, workspace_key)
        async with _optional_workspace_lease(workspace_key, is_threaded=is_threaded):
            drive = await cls.acquire(thread_id=thread_id, workspace_key=workspace_key)
            yield drive

    @classmethod
    async def acquire(cls, *, thread_id: str | None, workspace_key: str | None = None) -> DeepAgentSession:
        """Acquire (create-or-reattach) the session for a drive and materialize its creds.

        ``require_sandbox()`` is the ONE raising chokepoint — a box with no provider raises
        :class:`~tai42_contract.sandbox.SandboxUnavailableError` here, so run/astream carry a
        HARD sandbox dependency (§B3.7). ``workspace_key`` is supplied on a resume (reattach the
        parked volume); otherwise it is derived from ``thread_id`` (threaded) or a fresh
        ``uuid4`` (tool-face).

        A THREADED run must reach here THROUGH :meth:`leased` (which holds the workspace lease
        around this call), so session creation + cred materialization on the shared volume stay
        serialized across workers; calling ``acquire`` directly on a threaded key is for tests that
        exercise the create/cred path without the cross-worker lease."""
        settings = langchain_deep_agent_settings()
        sandbox = tai42_app.sandboxes.require_sandbox()

        is_threaded = thread_id is not None
        workspace_key = cls._resolve_workspace_key(thread_id, workspace_key)
        durability = "persistent" if is_threaded else "ephemeral"

        env, bearer = await _resolve_creds(settings.creds)
        spec, _policy = build_policied_spec(
            image=settings.session_image,
            workspace_key=workspace_key,
            durability=durability,
            env=env,
            cpu=settings.cpu,
            memory_mb=settings.memory_mb,
            ttl_seconds=settings.session_ttl_seconds,
            labels={"tai42.agent": AGENT_NAME, "tai42.thread": thread_id or ""},
            network_setting=settings.network,
        )
        session = await sandbox.create_session(spec)
        drive = cls(
            session=session,
            settings=settings,
            workspace_key=workspace_key,
            is_threaded=is_threaded,
            bearer_creds=bearer,
        )
        await drive._materialize_bearer_creds()
        return drive

    @property
    def workspace_retention_horizon(self) -> datetime:
        """The latest wall-time this run's durable WORKSPACE volume is guaranteed to still
        hold it: ``now + session_ttl`` (the idle-reap horizon; the park write is activity that
        (re)starts the idle clock). Passed as the ``extra_retention_horizon`` so a park's bound
        is ``min(checkpoint, workspace)`` (§B3.1)."""
        return datetime.now(UTC) + timedelta(seconds=self._settings.session_ttl_seconds)

    async def scrub_credentials(self) -> None:
        """Remove the bearer credential MATERIAL under ``{ws}/.creds`` — the TERMINAL-exit scrub
        (normal, error, timeout, final-cancel), SKIPPED on a park-suspend (the run is still
        LIVE; the file stays for the door-less expiry resume to reuse, whose own terminal exit
        scrubs it). Honors the invariant: no injected credential material persists after the run
        reaches a TERMINAL state. An un-removable directory raises loudly (never a silent
        leave-behind)."""
        if not self._bearer_creds:
            return
        result = await self.session.exec(["rm", "-rf", _CREDS_DIR], timeout_seconds=30)
        if result.exit_code != 0:
            raise RuntimeError(
                f"langchain_deep_agent could not scrub bearer credential material under {_CREDS_DIR!r}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def _materialize_bearer_creds(self) -> None:
        """Write each resolved bearer cred as a credential-helper file under ``{ws}/.creds`` —
        RE-WRITTEN every turn from the fresh ``resolve_connection_auth`` resolution, so a
        refreshed token reaches a reused persistent session on the next turn. The material lands
        OUTSIDE ``{ws}/project`` (unreachable through the agent's file tools)."""
        for spec, resolved in self._bearer_creds:
            body = _credential_helper_body(resolved)
            await self.session.put_file(f"{_CREDS_DIR}/{spec.env_name}", body.encode("utf-8"))


def _credential_helper_body(resolved: dict[str, SecretStr]) -> str:
    """The credential-helper file body for one resolved bearer cred: an
    ``Authorization: Bearer <token>`` line for the OAuth token, plus any transport-partitioned
    static header lines injected alongside (never instead of the token)."""
    lines = [f"{name}: {value.get_secret_value()}" for name, value in resolved.items()]
    return "\n".join(lines) + "\n"


async def _resolve_creds(
    creds: list[SessionCredSpec],
) -> tuple[dict[str, SecretStr], list[tuple[ConnectionCred, dict[str, SecretStr]]]]:
    """Split the operator cred list into the create-time session ``env`` (static + connection
    ``delivery="env"`` values) and the per-turn bearer files to materialize.

    Each connection-reference entry resolves PER-CALLER via
    ``tai42_app.connectors.resolve_connection_auth`` (which RAISES on an identity-less door — the
    seam fail-close — and takes ``connection_id`` from operator settings). A resolution yielding
    nothing usable injects nothing; a ``required`` entry that resolves to nothing raises loudly.
    """
    env: dict[str, SecretStr] = {}
    bearer: list[tuple[ConnectionCred, dict[str, SecretStr]]] = []
    for spec in creds:
        if isinstance(spec, StaticCred):
            env[spec.env_name] = spec.value
            continue

        resolved = await tai42_app.connectors.resolve_connection_auth(
            spec.connection_id, spec.provider_id, spec.sub_service
        )
        material = _collect_material(resolved)
        if not material:
            if spec.required:
                raise RuntimeError(
                    f"required session cred {spec.env_name!r} resolved to no usable credential "
                    f"(connection {spec.connection_id!r})"
                )
            continue

        if spec.delivery == "env":
            # STATIC-only channel: bake the resolved values into the clean session env at create.
            env.update(material)
        else:
            bearer.append((spec, material))
    return env, bearer


def _collect_material(resolved: object) -> dict[str, SecretStr]:
    """Flatten a :class:`~tai42_contract.connectors.ResolvedConnectionAuth` into a
    ``{name: SecretStr}`` map across its three channels (``access_token`` → ``Authorization``,
    plus static ``env``/``headers``), or an empty map when it injects nothing."""
    if resolved is None:
        return {}
    material: dict[str, SecretStr] = {}
    token = getattr(resolved, "access_token", None)
    if token is not None:
        material["Authorization"] = SecretStr(f"Bearer {token.get_secret_value()}")
    for name, value in dict(getattr(resolved, "env", {})).items():
        material[name] = value
    for name, value in dict(getattr(resolved, "headers", {})).items():
        material[name] = value
    return material
