"""Direct/host sandbox provider for the TAI ecosystem.

Importing this package registers the :class:`LocalSandbox` provider on the global
``tai42_app`` handle as a side effect. The host names this package in its
manifest's ``sandbox_module`` field and imports it at startup; exactly one sandbox
provider is active per deployment, and the operator picks direct/host execution by
installing this provider.
"""

from tai42_sandbox_local.provider import LocalSandbox
from tai42_sandbox_local.settings import SandboxLocalSettings, sandbox_local_settings

__all__ = ["LocalSandbox", "SandboxLocalSettings", "sandbox_local_settings"]
