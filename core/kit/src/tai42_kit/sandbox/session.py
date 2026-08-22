"""``ManagedSandboxSession`` — the session bookkeeping base, written once.

The kit ships a session base alongside :class:`~tai42_kit.sandbox.base.ManagedSandbox`
so a provider never reimplements the ledger-backed :class:`SandboxSession`
bookkeeping. A provider subclasses this and implements ONLY its runtime I/O
(``workspace_path``, ``exec``, ``exec_start``, ``put_file``, ``get_file``); it
inherits ``id`` / ``info()`` / ``destroy()`` / ``touch()``, all of which route
through the owning :class:`ManagedSandbox` so teardown and TTL always pass the
ledger, never a provider bypass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.sandbox import SandboxSession, SandboxSessionInfo

if TYPE_CHECKING:
    from tai42_kit.sandbox.base import ManagedSandbox


class ManagedSandboxSession(SandboxSession):
    """Ledger-backed :class:`SandboxSession` base every provider session extends.

    Holds only its id and a handle to the owning :class:`ManagedSandbox`; every
    piece of observable state (``info()``) and every lifecycle turn (``destroy()``,
    ``touch()``) reads or mutates the sandbox's ledger, so a provider adds nothing
    to session bookkeeping.
    """

    def __init__(self, *, sandbox: ManagedSandbox, session_id: str) -> None:
        self._sandbox = sandbox
        self._session_id = session_id

    @property
    def id(self) -> str:
        return self._session_id

    async def info(self) -> SandboxSessionInfo:
        return self._sandbox.session_info(self._session_id)

    async def touch(self) -> None:
        self._sandbox.extend_session(self._session_id)

    async def destroy(self) -> None:
        await self._sandbox.destroy_session(self._session_id)
