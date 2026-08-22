"""The sandbox ABCs — the provider face, one session, and one interactive exec.

The app core depends only on these interfaces and stays sandbox-agnostic;
concrete providers implement them (the kit ships a :class:`Sandbox` /
:class:`SandboxSession` base carrying the shared ledger/TTL bookkeeping so a
provider writes only its runtime I/O) and register via
``@tai42_app.sandboxes.register_sandbox``. The contract carries no logic.

PATH CONTRACT (``exec``/``exec_start`` ``cwd`` and ``put_file``/``get_file``
``path``): a value is WORKSPACE-RELATIVE by DEFAULT — the provider resolves a
relative value against ``workspace_path`` with realpath containment, and an unset
``cwd`` defaults to ``workspace_path``. An ABSOLUTE value remains allowed but is
the CALLER's own responsibility. A consumer needing an absolute path builds it
from ``session.workspace_path``, never a hardcoded root. A direct-host
(``isolation="none"``) provider MAY reject a file-transfer ``path`` that resolves
outside the workspace — containment is a permitted tightening where the provider
itself performs the I/O; an absolute path built from ``workspace_path`` is accepted
by every provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from pydantic import SecretStr

from tai42_contract.sandbox.models import (
    ExecResult,
    SandboxSessionInfo,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)


class SandboxExecHandle(ABC):
    """A live interactive exec started by :meth:`SandboxSession.exec_start`.

    CONCURRENCY CONTRACT: :meth:`write_stdin` / :meth:`close_stdin` MUST be safe
    to call concurrently with active :attr:`output` iteration — a provider that
    serializes reads and writes on one attach stream does its OWN demux/buffering
    (a deadlocking handle is non-conformant). A single :meth:`write_stdin` call
    delivers its bytes intact and in order (the provider must not split or reorder
    them); a consumer multiplexing a line protocol holds its OWN single-writer
    lock so each message is one atomic call — the provider does no framing.

    LIFETIME CONTRACT: after the exec has exited, :meth:`kill` is idempotent (a
    safe no-op) and :meth:`write_stdin` raises a typed :class:`SandboxError`
    (never an arbitrary exception).
    """

    @abstractmethod
    async def write_stdin(self, data: bytes) -> None:
        """Deliver ``data`` to the exec's stdin intact and in order."""

    @abstractmethod
    async def close_stdin(self) -> None:
        """Signal end-of-input to the exec."""

    @property
    @abstractmethod
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        """The interleaved stdout/stderr stream, terminated by one
        :class:`SandboxStreamExit`. On ``timeout_seconds`` expiry the provider
        kills the exec and the iterator raises :class:`SandboxExecTimeoutError`."""

    @abstractmethod
    async def kill(self) -> None:
        """Terminate the exec. Idempotent once the exec has exited."""


class SandboxSession(ABC):
    """One live sandbox session — the unit a consumer runs code in."""

    @property
    @abstractmethod
    def id(self) -> str:
        """This session's provider-assigned id."""

    @property
    @abstractmethod
    def workspace_path(self) -> str:
        """The provider's ABSOLUTE root path for THIS session's workspace.

        Also carried on :class:`SandboxSessionInfo` so a caller can read it off
        ``info()`` too. Anchors the workspace-relative resolution of ``cwd`` /
        ``path`` (see the module path contract)."""

    @abstractmethod
    async def info(self) -> SandboxSessionInfo:
        """This session's observable state."""

    @abstractmethod
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        """Run ``argv`` to completion and return its :class:`ExecResult`.

        ``timeout_seconds`` is REQUIRED: on expiry the provider kills the exec and
        raises :class:`SandboxExecTimeoutError`. ``env`` overlays the session's
        base ``spec.env`` (per-exec keys override on collision). ``cwd`` is
        WORKSPACE-RELATIVE by default (resolved against ``workspace_path``; unset
        defaults to ``workspace_path``) per the module path contract."""

    @abstractmethod
    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        """Start ``argv`` as an INTERACTIVE exec, returning a
        :class:`SandboxExecHandle`.

        ``timeout_seconds`` is REQUIRED: on expiry the provider kills the exec and
        the handle's ``output`` iterator raises :class:`SandboxExecTimeoutError`.
        ``env`` and ``cwd`` follow the same rules as :meth:`exec`."""

    @abstractmethod
    async def put_file(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` (WORKSPACE-RELATIVE by default) in the
        workspace."""

    @abstractmethod
    async def get_file(self, path: str) -> bytes:
        """Read ``path`` (WORKSPACE-RELATIVE by default) from the workspace. Raise
        a typed :class:`SandboxError` on a miss."""

    @abstractmethod
    async def touch(self) -> None:
        """Extend ``expires_at`` by the session's ttl — a keep-alive turn."""

    @abstractmethod
    async def destroy(self) -> None:
        """Tear this session down."""


class Sandbox(ABC):
    """Abstract sandbox provider for a Tai app.

    A provider creates/destroys disposable :class:`SandboxSession` instances and
    reaps expired ones. The app core depends only on this interface and stays
    sandbox-agnostic; concrete providers (a container runtime, a direct-host
    runner) implement it and register via
    ``@tai42_app.sandboxes.register_sandbox``.
    """

    @abstractmethod
    async def create_session(self, spec: SandboxSessionSpec) -> SandboxSession:
        """Create a session from ``spec`` or REJECT it with
        :class:`SandboxSpecRejectedError`."""

    @abstractmethod
    async def get_session(self, session_id: str) -> SandboxSession:
        """Fetch a live session by id. Raise
        :class:`SandboxSessionNotFoundError` if absent."""

    @abstractmethod
    async def list_sessions(self) -> list[SandboxSessionInfo]:
        """The observable state of every live session."""

    @abstractmethod
    async def destroy_session(self, session_id: str) -> None:
        """Tear a session down. Idempotent on an already-gone session."""

    @abstractmethod
    async def reap(self) -> list[str]:
        """Destroy every session past its ``expires_at`` and return the destroyed
        ids."""
