"""The host-subprocess session.

``LocalSandboxSession`` implements only the runtime I/O the contract asks of a
:class:`~tai42_contract.sandbox.SandboxSession` — ``workspace_path``, ``exec``,
``exec_start``, ``put_file``, ``get_file`` — over a plain host subprocess (a
:class:`~tai42_kit.sandbox.HostProcess`, its interactive exec a
:class:`~tai42_kit.sandbox.HostExecHandle`) and the host filesystem; the ledger/TTL
bookkeeping (``id`` / ``info`` / ``touch`` / ``destroy``) is inherited from
:class:`~tai42_kit.sandbox.ManagedSandboxSession`.

SECURITY INVARIANTS held here:

- CLEAN env — a subprocess is spawned with a fixed ``PATH`` base plus the session's
  ``spec.env`` and any per-exec ``env`` overlay, NEVER the host ``os.environ``. A
  ``SecretStr`` value is unwrapped ONLY at the spawn call, never into a repr/log/error.
- REALPATH CONTAINMENT — a workspace-relative ``cwd`` / ``put_file`` / ``get_file``
  path resolves against ``workspace_path`` and a resolved path escaping the workspace
  root raises loudly, so a ``..`` component cannot walk out of the workspace.
- LOUD failure — every host error surfaces as a typed
  :class:`~tai42_contract.sandbox.SandboxError`; the only swallowed case is the
  contract's idempotent no-op on destroy (an already-gone directory). A kill reaches a
  process group only while the kit's anchor holds it (:class:`~tai42_kit.sandbox.HostProcess`),
  so a released group is never signalled and a kill is never a shot at a reused id.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxDurability,
    SandboxError,
    SandboxExecHandle,
    SandboxFileNotFoundError,
)
from tai42_kit.sandbox import ExecDeadline, HostExecHandle, HostProcess, ManagedSandboxSession, run_host_process

if TYPE_CHECKING:
    from tai42_sandbox_local.provider import LocalSandbox


class LocalSandboxSession(ManagedSandboxSession):
    """One host-subprocess session over a workspace directory on the host filesystem."""

    def __init__(
        self,
        *,
        sandbox: LocalSandbox,
        session_id: str,
        workspace_path: str,
        durability: SandboxDurability,
        base_path: str,
        spec_env: dict[str, SecretStr],
        teardown_dir: str,
        sidecar_path: str | None,
    ) -> None:
        """Bind the session to its realpath-resolved ``workspace_path``, base env, and teardown paths."""
        super().__init__(sandbox=sandbox, session_id=session_id)
        # The ABSOLUTE, realpath-resolved workspace root (equal to the child's cwd for
        # an unset cwd, and the anchor for workspace-relative path resolution).
        self._workspace_path = workspace_path
        self._durability: SandboxDurability = durability
        self._base_path = base_path
        self._spec_env = dict(spec_env)
        # The directory to remove on teardown (the workspace for ephemeral, the named
        # persistent dir otherwise) and the sidecar to remove alongside a persistent one.
        self._teardown_dir = teardown_dir
        self._sidecar_path = sidecar_path
        self._handles: list[HostExecHandle] = []

    @property
    def workspace_path(self) -> str:
        """The absolute, realpath-resolved workspace root on the host filesystem."""
        return self._workspace_path

    @property
    def durability(self) -> SandboxDurability:
        """This session's durability (``ephemeral`` or ``persistent``)."""
        return self._durability

    @property
    def teardown_dir(self) -> str:
        """The directory removed on teardown (the workspace for ephemeral, the named persistent dir otherwise)."""
        return self._teardown_dir

    @property
    def sidecar_path(self) -> str | None:
        """The sidecar file removed alongside a persistent workspace, or ``None`` for an ephemeral session."""
        return self._sidecar_path

    # -- env + path resolution ----------------------------------------------------

    def _build_env(self, env: dict[str, SecretStr] | None) -> dict[str, str]:
        """The CLEAN subprocess env: the fixed ``PATH`` base, overlaid by ``spec.env`` then per-exec ``env``.

        Per-exec keys win, a supplied ``PATH`` wins over the base. SecretStr values are
        unwrapped ONLY here, never logged.
        """
        merged: dict[str, str] = {"PATH": self._base_path}
        for key, value in self._spec_env.items():
            merged[key] = value.get_secret_value()
        if env:
            for key, value in env.items():
                merged[key] = value.get_secret_value()
        return merged

    def _resolve_cwd(self, cwd: str | None) -> str:
        """The child's working directory.

        Unset defaults to the workspace root; a relative value resolves WITHIN the
        workspace with realpath containment; an ABSOLUTE value is allowed as given — this
        provider is isolation=none, so the subprocess already has full host reach and
        containing an absolute cwd adds no security.
        """
        if cwd is None:
            return self._workspace_path
        if os.path.isabs(cwd):
            return cwd
        return self._contained_path(cwd)

    def _contained_path(self, path: str) -> str:
        """Resolve a workspace-relative ``path`` against the workspace root, rejecting an escape.

        A resolved path that escapes the root is refused (realpath containment).
        """
        root = self._workspace_path
        resolved = os.path.realpath(os.path.join(root, path))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SandboxError(f"path {path!r} escapes the sandbox workspace")
        return resolved

    # -- exec ---------------------------------------------------------------------

    async def _spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdin: bool,
    ) -> HostProcess:
        """Spawn a host subprocess in its own session/process-group, anchored by the kit.

        The anchor lets a later kill reach what the command leaves in its group. A spawn
        failure (a missing binary, an unwritable cwd, an env the host cannot hold) raises a
        typed error NAMING the binary, never any env value.
        """
        if not argv:
            raise SandboxError("sandbox exec requires a non-empty argv")
        try:
            return await HostProcess.spawn(argv, cwd=cwd, env=env, stdin=stdin)
        except (OSError, ValueError) as exc:
            raise SandboxError(f"sandbox exec could not spawn {argv[0]!r}: {exc}") from exc

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

        A host-side timeout kills the process and raises :class:`SandboxExecTimeoutError`; any other
        failure (a failed stdin write, a cancelled call) kills it and propagates.
        """
        process = await self._spawn(
            argv,
            cwd=self._resolve_cwd(cwd),
            env=self._build_env(env),
            stdin=stdin is not None,
        )
        return await run_host_process(process, stdin=stdin, timeout_seconds=timeout_seconds)

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        """Start ``argv`` as a live interactive subprocess and return its :class:`SandboxExecHandle`.

        The exec's ``timeout_seconds`` deadline is fixed as it is spawned, so the handle's output
        is bounded by what is left of it whenever the caller first reads.
        """
        deadline = ExecDeadline.from_now(timeout_seconds)
        process = await self._spawn(
            argv,
            cwd=self._resolve_cwd(cwd),
            env=self._build_env(env),
            stdin=True,
        )
        handle = HostExecHandle(process, deadline=deadline)
        self._handles.append(handle)
        return handle

    # -- file transfer ------------------------------------------------------------

    async def put_file(self, path: str, data: bytes) -> None:
        """Write ``data`` under the workspace, with realpath containment enforced.

        UNLIKE ``cwd``, file transfer is I/O the PROVIDER performs on the host, so realpath
        containment is enforced unconditionally: an absolute ``path`` built from
        ``workspace_path`` passes, but an absolute ``path`` resolving OUTSIDE the workspace
        is refused loudly because on a host provider it would be a real host write (a
        permitted tightening — see the contract path invariant).
        """
        target = Path(self._contained_path(path))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise SandboxError(f"sandbox put_file failed for {path!r}: {exc}") from exc

    async def get_file(self, path: str) -> bytes:
        """Read ``path`` from the workspace, with realpath containment enforced.

        Containment is enforced unconditionally for the same reason as :meth:`put_file`:
        this provider performs a real host read, so an absolute ``path`` built from
        ``workspace_path`` passes while one resolving OUTSIDE the workspace is refused
        loudly.
        """
        target = Path(self._contained_path(path))
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise SandboxFileNotFoundError(path) from exc
        except OSError as exc:
            raise SandboxError(f"sandbox get_file failed for {path!r}: {exc}") from exc

    # -- teardown -----------------------------------------------------------------

    async def terminate_handles(self) -> None:
        """Kill every interactive subprocess this session started, and whatever each left running in its process group.

        Idempotent on an already-killed handle. Called by the provider teardown primitive.
        """
        for handle in self._handles:
            await handle.kill()
