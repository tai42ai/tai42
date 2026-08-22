"""The direct/host sandbox provider — ``LocalSandbox``.

Declarations only at import: the concrete :class:`~tai42_kit.sandbox.ManagedSandbox`
subclass and its registration on the global ``tai42_app`` handle. Everything
host-shaped around the runtime — the session ledger, TTL bookkeeping, generic
``reap`` / ``destroy_session``, orphan recovery, and the session-create POLICY
CHOKEPOINT — comes from the shared kit base; this provider implements only the
three runtime primitives (create/destroy/list-orphan resources).

PROVIDER-CAPABILITY HALF of the policy (the kit seam already enforced the operator
policy floor/ceiling before the primitive runs): this direct-host provider gives
NO isolation and confines NO network, so it accepts EXACTLY what it can honestly
enforce and REJECTS the rest LOUDLY, never silently degrading —

- isolation ``"none"`` accepted; ``"container"`` / ``"vm"`` rejected (no
  namespace/container/VM machinery — a silent downgrade would be the exact
  isolation-loss the floor rule forbids);
- network ``"egress"`` accepted; ``"none"`` / ``"internal"`` rejected (a bare host
  process cannot be confined to no-network / internal-only without machinery this
  mode deliberately omits);
- a resource cap (``cpu`` / ``memory_mb``) rejected (the direct mode has no
  cgroup/rlimit cap machinery);
- ``spec.image`` is INERT — the host itself is the execution environment (the
  operator installs the runtime on the host); the requested reference is recorded
  in the sidecar metadata for traceability, never used to govern the runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime

from tai42_contract.app import tai42_app
from tai42_contract.sandbox import (
    SandboxError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)
from tai42_contract.sandbox.models import WORKSPACE_KEY_RE
from tai42_kit.sandbox import ManagedSandbox, ManagedSandboxSession

from tai42_sandbox_local.sessions import LocalSandboxSession
from tai42_sandbox_local.settings import SandboxLocalSettings, sandbox_local_settings

# The dot-prefixed control subdirectories under the root. The workspace-key charset
# forbids a leading dot, so neither can ever be a real workspace name; the orphan
# scan skips them explicitly so they are never mistaken for an orphan workspace.
_EPHEMERAL_SUBDIR = ".ephemeral"
_SIDECAR_SUBDIR = ".tai-sandbox"


class LocalSandbox(ManagedSandbox):
    """Direct/host sandbox provider: runs a session's code as a plain host subprocess.

    No container, no isolation. The operator picks this execution mode by installing
    this provider instead of a container provider; there is no dual code path in the
    consumers.
    """

    def _settings(self) -> SandboxLocalSettings:
        """Read the provider settings LIVE (``root`` is recycle-class, so a settings
        epoch flip is honored on the next create rather than frozen at import)."""
        return sandbox_local_settings()

    # -- create -------------------------------------------------------------------

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        self._reject_unenforceable(spec)
        settings = self._settings()
        root = settings.root

        if spec.durability == "persistent":
            teardown_dir = os.path.join(root, spec.workspace_key)
            self._make_workspace(teardown_dir, root)
            sidecar_path = self._write_sidecar(root, spec)
        else:
            ephemeral_parent = os.path.join(root, _EPHEMERAL_SUBDIR)
            self._make_workspace(ephemeral_parent, root)
            try:
                teardown_dir = tempfile.mkdtemp(dir=ephemeral_parent)
            except OSError as exc:
                raise SandboxError(f"sandbox workspace root {root!r} is not writable: {exc}") from exc
            sidecar_path = None

        return LocalSandboxSession(
            sandbox=self,
            session_id=uuid.uuid4().hex,
            workspace_path=os.path.realpath(teardown_dir),
            durability=spec.durability,
            base_path=settings.base_path,
            spec_env=dict(spec.env),
            teardown_dir=teardown_dir,
            sidecar_path=sidecar_path,
        )

    def _reject_unenforceable(self, spec: SandboxSessionSpec) -> None:
        """REJECT loudly every spec facet this direct-host provider cannot honor.

        ``spec`` arrives policy-resolved, so ``isolation`` is the concrete effective
        tier (never ``None``)."""
        if spec.isolation != "none":
            raise SandboxSpecRejectedError(
                f"provider cannot enforce: isolation {spec.isolation!r} — the direct/host provider "
                "gives no isolation and never downgrades a container/vm request to a bare host process "
                "(use a container provider for enforced isolation)"
            )
        if spec.network != "egress":
            raise SandboxSpecRejectedError(
                f"provider cannot enforce: network {spec.network!r} — the direct/host provider runs on the "
                "host network and cannot confine a bare host process to none/internal (use a container "
                "provider for network lockdown)"
            )
        if spec.cpu is not None or spec.memory_mb is not None:
            raise SandboxSpecRejectedError(
                "provider cannot enforce: a cpu/memory_mb cap — the direct/host provider has no "
                "cgroup/rlimit cap machinery and never runs a capped request uncapped"
            )

    def _make_workspace(self, path: str, root: str) -> None:
        """Create ``path`` idempotently (adopt-on-race), or raise a typed error naming
        the configured root — NEVER a silent downgrade to a scratch dir."""
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise SandboxError(f"sandbox workspace root {root!r} is not writable: {exc}") from exc

    def _write_sidecar(self, root: str, spec: SandboxSessionSpec) -> str:
        """Persist a persistent session's kit label set OUTSIDE the workspace tree, so
        the workspace stays pristine for the agent and an orphan sweep after a recycle
        can rediscover the durable workspace. A pre-existing sidecar is left as-is
        (adopt-on-race)."""
        sidecar_dir = os.path.join(root, _SIDECAR_SUBDIR)
        self._make_workspace(sidecar_dir, root)
        sidecar_path = os.path.join(sidecar_dir, f"{spec.workspace_key}.json")
        if os.path.exists(sidecar_path):
            return sidecar_path
        payload = {
            "workspace_key": spec.workspace_key,
            "durability": spec.durability,
            "created_at": datetime.now(UTC).isoformat(),
            # The requested image is INERT under this provider but recorded verbatim so
            # the durable workspace's traceability shows what the consumer asked for.
            "image": spec.image,
            "labels": dict(spec.labels),
        }
        try:
            with open(sidecar_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        except OSError as exc:
            raise SandboxError(f"sandbox workspace root {root!r} is not writable: {exc}") from exc
        return sidecar_path

    # -- teardown -----------------------------------------------------------------

    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        local = self._as_local(session)
        await local.terminate_handles()
        if local.durability == "ephemeral":
            # Scratch dies on BOTH paths.
            self._remove_tree(local.teardown_dir)
            return
        if remove_workspace:
            # Explicit teardown removes the named persistent dir + its sidecar; a reap
            # (remove_workspace=False) keeps them.
            self._remove_tree(local.teardown_dir)
            if local.sidecar_path is not None:
                self._remove_file(local.sidecar_path)

    def _as_local(self, session: ManagedSandboxSession) -> LocalSandboxSession:
        if not isinstance(session, LocalSandboxSession):  # pragma: no cover - the base only holds our sessions
            raise SandboxError("sandbox teardown received a foreign session type")
        return session

    def _remove_tree(self, path: str) -> None:
        """Remove a directory tree. A missing directory is the contract's idempotent
        no-op; a genuine OSError (e.g. a permission failure) still raises typed."""
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SandboxError(f"sandbox teardown failed to remove {path!r}: {exc}") from exc

    def _remove_file(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SandboxError(f"sandbox teardown failed to remove {path!r}: {exc}") from exc

    # -- orphan recovery ----------------------------------------------------------

    async def _list_orphan_resources(self) -> list[str]:
        """List persistent workspaces the live ledger does not know (LEFT in place —
        durable state) and destroy leftover ephemeral scratch dirs (a crashed
        process's residue). The dot-prefixed control subdirs are skipped so they are
        never mistaken for orphan workspaces."""
        root = self._settings().root
        if not os.path.isdir(root):
            return []

        known = set(self._by_workspace)
        descriptors: list[str] = []
        for entry in sorted(os.listdir(root)):
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            if entry in (_EPHEMERAL_SUBDIR, _SIDECAR_SUBDIR):
                continue
            # The workspace-key charset forbids a leading dot, so any non-matching name
            # is not a managed workspace and is never treated as an orphan.
            if not WORKSPACE_KEY_RE.fullmatch(entry) or entry in known:
                continue
            descriptors.append(f"persistent workspace kept (no live session): {full}")

        ephemeral_parent = os.path.join(root, _EPHEMERAL_SUBDIR)
        if os.path.isdir(ephemeral_parent):
            for entry in sorted(os.listdir(ephemeral_parent)):
                full = os.path.join(ephemeral_parent, entry)
                if os.path.isdir(full):
                    self._remove_tree(full)
                    descriptors.append(f"ephemeral scratch removed (crashed process): {full}")
        return descriptors


# Plain call (not a decorator) so ``LocalSandbox`` keeps its concrete class type.
tai42_app.sandboxes.register_sandbox(LocalSandbox)
