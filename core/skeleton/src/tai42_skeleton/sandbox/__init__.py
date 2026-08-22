"""Sandbox feature: the disposable-execution-environment registration seam.

The :class:`~tai42_contract.sandbox.Sandbox` ABC is the contract; concrete providers
(a container runtime, a direct-host runner) are external plugins that extend the kit
:class:`~tai42_kit.sandbox.ManagedSandbox` and register via
``@tai42_app.sandboxes.register_sandbox``. This package owns the process holder, the ONE
shared policy resolver both the kit bind and the read accessors flow through, and the
periodic reap loop.
"""

from tai42_skeleton.sandbox.policy import resolve_sandbox_policy
from tai42_skeleton.sandbox.reaper import run_sandbox_reap_loop
from tai42_skeleton.sandbox.registry import SandboxHolder

__all__ = [
    "SandboxHolder",
    "resolve_sandbox_policy",
    "run_sandbox_reap_loop",
]
