"""Docker sandbox provider for the TAI ecosystem.

Importing this package registers the :class:`DockerSandbox` provider on the global
``tai42_app`` handle as a side effect. The host names this package in its manifest's
``sandbox_module`` field and imports it at startup; the provider then drives a REMOTE
Docker engine over the Docker Engine API, running each session as a hardened per-session
container that mounts only its own workspace volume.
"""

from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.sessions import DockerSandboxExecHandle, DockerSandboxSession

__all__ = ["DockerSandbox", "DockerSandboxExecHandle", "DockerSandboxSession"]
