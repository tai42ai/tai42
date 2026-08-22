"""Sandbox registration — the impl body behind the ``app.sandboxes`` facet.

Mirrors :class:`~tai42_skeleton.backend.registry.BackendHolder`: holds the process's
single registered :class:`~tai42_contract.sandbox.Sandbox` provider. A provider registers
through the ``@tai42_app.sandboxes.register_sandbox`` decorator, which instantiates it AND
binds the operator-resolved :class:`~tai42_contract.sandbox.SandboxPolicy` onto the
instance via the kit ``bind_policy`` — the ONLY path the policy reaches the kit
session-create chokepoint, since the kit cannot read ``CoreSettings``. Acquisition is the
raising ``require`` chokepoint; the nullable ``sandbox`` read is status-only and never
gates execution. Unlike the backend holder there is NO ``launch`` verb — a sandbox is not
launched at boot, its sessions are created on demand by consumers.
"""

from __future__ import annotations

from tai42_contract.sandbox import Sandbox, SandboxUnavailableError
from tai42_kit.sandbox import ManagedSandbox

from tai42_skeleton.sandbox.policy import resolve_sandbox_policy


class SandboxHolder:
    """Holds the process's single registered :class:`Sandbox` provider instance."""

    def __init__(self) -> None:
        self._sandbox: Sandbox | None = None

    @property
    def sandbox(self) -> Sandbox | None:
        return self._sandbox

    def register_sandbox(self, cls: type[Sandbox]) -> type[Sandbox]:
        """Instantiate and store ``cls`` as the provider, binding the resolved policy.

        A second registration within one boot is a genuine conflict (two modules
        claiming the scalar slot) — fail loudly rather than last-write-win. The
        resolved :class:`SandboxPolicy` is bound onto the instance via the kit
        ``bind_policy`` so the kit session-create chokepoint holds it; a provider that
        does not extend :class:`ManagedSandbox` cannot carry the bind and is refused
        here rather than silently unenforced."""
        if self._sandbox is not None:
            raise RuntimeError(
                f"a sandbox provider is already registered ({type(self._sandbox).__module__}."
                f"{type(self._sandbox).__qualname__}); the scalar sandbox slot holds exactly one"
            )
        instance = cls()
        if not isinstance(instance, ManagedSandbox):
            raise TypeError(
                f"sandbox provider {cls.__module__}.{cls.__qualname__} must extend "
                "tai42_kit.sandbox.ManagedSandbox so the operator policy binds onto it"
            )
        instance.bind_policy(resolve_sandbox_policy())
        self._sandbox = instance
        return cls

    def require(self) -> Sandbox:
        """Return the registered provider or raise :class:`SandboxUnavailableError`.

        The ONE acquisition chokepoint every consumer reaches — a constant-message,
        loud raise naming the selecting setting and the manifest field when no provider
        backs the slot, never a silent ``None``."""
        if self._sandbox is None:
            raise SandboxUnavailableError(
                "no sandbox provider is registered: set the manifest 'sandbox_module' scalar slot "
                "(selected via TAI_MCP_SANDBOX) to a package that registers one"
            )
        return self._sandbox
