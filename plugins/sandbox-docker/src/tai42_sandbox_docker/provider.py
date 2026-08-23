"""The Docker :class:`~tai42_kit.sandbox.ManagedSandbox` implementation.

Registers the :class:`DockerSandbox` provider as an import side effect. The kit base
owns the session ledger, TTL bookkeeping, generic ``reap`` / ``destroy_session``, the
orphan-recovery hook, and the session-create policy chokepoint; this module implements
only the runtime PRIMITIVES the base calls — creating, tearing down, and listing the
engine resources of a session — plus the neutral-spec → engine-payload mapping.

It drives a REMOTE Docker engine over the Docker Engine API (aiodocker over aiohttp):
it spawns no local process, holds no host socket, and mounts no host path. A session
container is hardened (``no-new-privileges``, all capabilities dropped, unprivileged)
and mounts ONLY its own workspace volume, so it can never read the mTLS client identity
that speaks the engine control API.
"""

from __future__ import annotations

import json
import re
import ssl
from typing import TYPE_CHECKING, Any

import aiohttp  # pyright: ignore[reportMissingImports]
from aiodocker import Docker  # pyright: ignore[reportMissingImports]
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import (
    SandboxError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)
from tai42_kit.sandbox import (
    LABEL_DURABILITY,
    LABEL_SANDBOX,
    LABEL_WORKSPACE,
    ManagedSandbox,
)

from tai42_sandbox_docker.sessions import (
    WORKSPACE_PATH,
    DockerSandboxSession,
    engine_error,
)
from tai42_sandbox_docker.settings import DockerSandboxSettings, docker_sandbox_settings

if TYPE_CHECKING:
    from tai42_kit.sandbox import ManagedSandboxSession

# The shared stem of a persistent session's container and durable volume, keyed by
# ``workspace_key`` so two workers on one key converge on ONE container over ONE volume.
RESOURCE_PREFIX = "tai-sbx-"

# The idle init: real work is driven through the exec API, never the container's main
# process, so PID 1 just sleeps and keeps the session container alive.
IDLE_COMMAND = ("sleep", "infinity")

# The isolated inner bridge a ``network="internal"`` session joins — a docker network
# created with the ``internal`` flag has no gateway to the outside.
INTERNAL_NETWORK = "tai-sbx-internal"

# The neutral network tier → engine NetworkMode. ``egress`` joins the engine's default
# NAT'd bridge (the deployment's egress firewall NATs it out); ``internal`` joins an isolated
# bridge with no external routing; ``none`` attaches no network beyond loopback.
_NETWORK_MODES = {"none": "none", "egress": "bridge", "internal": INTERNAL_NETWORK}

_NANO = 1_000_000_000
_MEBI = 1024 * 1024

# A ``tcp://``/``http://`` scheme carrying an mTLS control API. aiodocker upgrades
# such a scheme to ``https://`` ONLY when it builds the SSL context itself (from
# DOCKER_TLS_VERIFY or a docker context); with a caller-supplied ssl_context it
# leaves the scheme untouched and would dial PLAINTEXT against the TLS port — the
# daemon then answers "Client sent an HTTP request to an HTTPS server". We hand
# aiodocker a normalized ``https://`` URL so an operator's canonical ``tcp://``
# endpoint speaks mTLS without them having to write ``https://``.
_TLS_SCHEME_RX = re.compile(r"^(?:tcp|http)://")


def resolve_engine_url(host: str, *, tls: bool) -> str:
    """The URL to hand aiodocker for ``host``.

    ``unix://``/``npipe://`` and a bare filesystem path address a local socket and
    pass through unchanged (a bare path becomes ``unix://<path>``). A ``tcp://`` /
    ``http://`` endpoint under mTLS (``tls`` true — cert paths in force) is
    normalized to ``https://`` so aiodocker actually runs TLS over it; without TLS
    the scheme is left as-is for aiodocker's own handling.
    """
    if host.startswith(("unix://", "npipe://")):
        return host
    if host.startswith("/"):
        return f"unix://{host}"
    if tls:
        return _TLS_SCHEME_RX.sub("https://", host)
    return host


def container_name(workspace_key: str) -> str:
    """The deterministic engine container name for ``workspace_key``."""
    return f"{RESOURCE_PREFIX}{workspace_key}"


def volume_name(workspace_key: str) -> str:
    """The deterministic engine volume name for ``workspace_key``."""
    return f"{RESOURCE_PREFIX}{workspace_key}"


def _reject_vm(isolation: str | None) -> None:
    """A ``vm`` floor is above the container boundary this provider can give — reject
    it loudly rather than silently run a weaker tier."""
    if isolation == "vm":
        raise SandboxSpecRejectedError(
            "provider offers container isolation; the requested 'vm' isolation floor is above what it can give"
        )


def _network_mode(network: str) -> str:
    mode = _NETWORK_MODES.get(network)
    if mode is None:
        raise SandboxSpecRejectedError(f"provider cannot express the network tier {network!r} on the configured engine")
    return mode


def _nanocpus(cpu: float) -> int:
    nanocpus = round(cpu * _NANO)
    if nanocpus <= 0:
        raise SandboxSpecRejectedError(f"cpu cap {cpu!r} is not a positive value the engine can enforce")
    return nanocpus


def _memory_bytes(memory_mb: int) -> int:
    memory_bytes = int(memory_mb) * _MEBI
    if memory_bytes <= 0:
        raise SandboxSpecRejectedError(f"memory_mb cap {memory_mb!r} is not a positive value the engine can enforce")
    return memory_bytes


def _workspace_mount(spec: SandboxSessionSpec) -> dict[str, Any]:
    """The ONE mount a session gets: its workspace volume at :data:`WORKSPACE_PATH`.

    A persistent session binds the named ``tai-sbx-<key>`` volume; an ephemeral one
    binds an anonymous volume (empty ``Source``) removed with the container. No host
    bind mount of any path is ever produced — the single-workspace-mount isolation
    invariant."""
    source = volume_name(spec.workspace_key) if spec.durability == "persistent" else ""
    return {"Target": WORKSPACE_PATH, "Source": source, "Type": "volume", "ReadOnly": False}


def build_container_config(
    spec: SandboxSessionSpec,
    *,
    default_cpu: float | None = None,
    default_memory_mb: int | None = None,
) -> dict[str, Any]:
    """Map a policy-resolved :class:`SandboxSessionSpec` onto the engine ContainerCreate
    payload, or REJECT what the provider cannot enforce.

    Hardening is stamped on EVERY container (``no-new-privileges``, all capabilities
    dropped, never privileged, a writable rootfs only so ``/workspace`` is writable),
    and the mount set is exactly the one workspace volume. A ``spec.env`` value is a
    ``SecretStr`` unwrapped ONLY here at the engine call."""
    _reject_vm(spec.isolation)

    host_config: dict[str, Any] = {
        "SecurityOpt": ["no-new-privileges"],
        "CapDrop": ["ALL"],
        "Privileged": False,
        "ReadonlyRootfs": False,
        "NetworkMode": _network_mode(spec.network),
        "Mounts": [_workspace_mount(spec)],
    }

    cpu = spec.cpu if spec.cpu is not None else default_cpu
    if cpu is not None:
        host_config["NanoCpus"] = _nanocpus(cpu)
    memory_mb = spec.memory_mb if spec.memory_mb is not None else default_memory_mb
    if memory_mb is not None:
        host_config["Memory"] = _memory_bytes(memory_mb)

    return {
        "Image": spec.image,
        "Cmd": list(IDLE_COMMAND),
        "WorkingDir": WORKSPACE_PATH,
        "Env": [f"{key}={value.get_secret_value()}" for key, value in spec.env.items()],
        "Labels": dict(spec.labels),
        "HostConfig": host_config,
    }


def _label_filter() -> str:
    """The engine list filter selecting resources carrying the reserved sandbox label."""
    return json.dumps({"label": [f"{LABEL_SANDBOX}=1"]})


class DockerSandbox(ManagedSandbox):
    """Docker sandbox provider: per-session containers on a REMOTE engine.

    Implements the kit base's runtime primitives against aiodocker; the base owns the
    ledger, TTL, reap, and the create-time policy chokepoint. A persistent workspace's
    named volume outlives the session and its reap; only an explicit ``destroy_session``
    removes it, and even then UNFORCED so the engine's "volume in use" guard blocks
    removal while another worker's container still mounts it.
    """

    def __init__(self, *, docker: Any | None = None, settings: DockerSandboxSettings | None = None) -> None:
        super().__init__()
        self._injected_client = docker
        self._settings_override = settings
        self._client: Any | None = None

    def _settings(self) -> DockerSandboxSettings:
        return self._settings_override or docker_sandbox_settings()

    async def _engine(self) -> Any:
        if self._client is None:
            if self._injected_client is not None:
                self._client = self._injected_client
            else:
                try:
                    self._client = self._create_engine()
                except (OSError, aiohttp.ClientError, DockerError) as exc:
                    raise self._connect_error(exc) from exc
        return self._client

    def _create_engine(self) -> Any:
        settings = self._settings()
        host = settings.host
        local = host.startswith(("unix://", "npipe://", "/"))
        # mTLS applies only to a remote tcp:// endpoint; a local socket carries no
        # client identity (and must not try to load one).
        ssl_context = None if local else (self._build_ssl_context(settings) if settings.tls_verify else None)
        url = resolve_engine_url(host, tls=ssl_context is not None)
        return Docker(url=url, ssl_context=ssl_context)

    def _build_ssl_context(self, settings: DockerSandboxSettings) -> ssl.SSLContext:
        """The mTLS client identity that speaks the engine control API. Loaded from the
        canonical ``/certs/client`` mount; a load failure names the paths, never their
        contents."""
        try:
            context = ssl.create_default_context(cafile=settings.tls_ca_path)
            # Loading a real client identity needs real certs — exercised by the live
            # conformance leg, not the unit gate.
            context.load_cert_chain(  # pragma: no cover
                certfile=settings.tls_cert_path, keyfile=settings.tls_key_path
            )
        except OSError as exc:
            raise SandboxError(
                f"cannot load the Docker engine mTLS client identity from {settings.tls_cert_path!r} / "
                f"{settings.tls_key_path!r} / {settings.tls_ca_path!r}"
            ) from exc
        return context  # pragma: no cover

    def _connect_error(self, exc: Exception) -> SandboxError:
        return SandboxError(
            f"cannot reach the Docker engine at the configured SANDBOX_DOCKER_HOST {self._settings().host!r} "
            f"({type(exc).__name__})"
        )

    # -- provider primitives -------------------------------------------------

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        settings = self._settings()
        config = build_container_config(
            spec,
            default_cpu=settings.default_cpu,
            default_memory_mb=settings.default_memory_mb,
        )
        docker = await self._engine()
        try:
            if spec.network == "internal":
                await self._ensure_internal_network(docker)
            await self._ensure_image(docker, spec.image, settings.pull_policy)
            if spec.durability == "persistent":
                await self._ensure_volume(docker, spec)
                container = await self._create_or_adopt(docker, config, spec.workspace_key)
            else:
                container = await docker.containers.create(config)
                await container.start()
        except DockerError as exc:
            # A runtime API error not already re-typed by a provisioning helper
            # (the ephemeral create/start, an adopt-path start) — surface it loudly.
            raise engine_error(exc) from exc
        except (OSError, aiohttp.ClientError) as exc:
            raise self._connect_error(exc) from exc
        return DockerSandboxSession(
            sandbox=self,
            session_id=container.id,
            container=container,
            workspace_key=spec.workspace_key,
            durability=spec.durability,
            base_env=spec.env,
        )

    async def _create_or_adopt(self, docker: Any, config: dict[str, Any], workspace_key: str) -> Any:
        """Idempotent create for a persistent workspace: adopt an existing container of
        the same name rather than racing a second one over the shared volume."""
        name = container_name(workspace_key)
        existing = await self._get_container(docker, name)
        if existing is not None:
            await self._ensure_running(existing)
            return existing
        try:
            container = await docker.containers.create(config, name=name)
        except DockerError as exc:
            if exc.status == 409:
                # Lost the name race to a concurrent create: adopt the winner.
                container = await docker.containers.get(name)
                await self._ensure_running(container)
                return container
            raise engine_error(exc) from exc
        await container.start()
        return container

    async def _get_container(self, docker: Any, name: str) -> Any | None:
        try:
            return await docker.containers.get(name)
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise engine_error(exc) from exc

    async def _ensure_running(self, container: Any) -> None:
        info = await container.show()
        if not info.get("State", {}).get("Running", False):
            await container.start()

    async def _ensure_volume(self, docker: Any, spec: SandboxSessionSpec) -> None:
        """Create (or adopt) the durable named volume. If the engine cannot provide a
        named volume the persistent request is REJECTED — never downgraded to ephemeral."""
        name = volume_name(spec.workspace_key)
        try:
            await docker.volumes.get(name)
            return
        except DockerError as exc:
            if exc.status != 404:
                raise engine_error(exc) from exc
        try:
            await docker.volumes.create(
                {
                    "Name": name,
                    "Labels": {
                        LABEL_SANDBOX: "1",
                        LABEL_WORKSPACE: spec.workspace_key,
                        LABEL_DURABILITY: "persistent",
                    },
                }
            )
        except DockerError as exc:
            raise SandboxSpecRejectedError(
                f"provider cannot honor a persistent workspace: the engine refused to create the "
                f"durable volume {name!r} ([{exc.status}] {exc.message})"
            ) from exc

    async def _ensure_image(self, docker: Any, image: str, pull_policy: str) -> None:
        try:
            await docker.images.inspect(image)
            return
        except DockerError as exc:
            if exc.status != 404:
                raise engine_error(exc) from exc
        if pull_policy == "never":
            raise SandboxError(
                f"image {image!r} is absent on the engine and pull_policy is 'never' (airgapped): refusing to pull"
            )
        try:
            await docker.images.pull(from_image=image)
        except DockerError as exc:
            raise engine_error(exc) from exc

    async def _ensure_internal_network(self, docker: Any) -> None:
        try:
            await docker.networks.get(INTERNAL_NETWORK)
            return
        except DockerError as exc:
            if exc.status != 404:
                raise engine_error(exc) from exc
        try:
            await docker.networks.create({"Name": INTERNAL_NETWORK, "Internal": True, "Labels": {LABEL_SANDBOX: "1"}})
        except DockerError as exc:
            if exc.status == 409:
                # A concurrent create won the name: adopt it.
                return
            raise engine_error(exc) from exc

    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        assert isinstance(session, DockerSandboxSession)
        docker = await self._engine()
        try:
            await session._container.delete(force=True, v=True)
        except DockerError as exc:
            # An already-gone container is a no-op ONLY on an explicit teardown; a reap
            # of a live ledger record still surfaces a genuine engine error.
            if not (remove_workspace and exc.status == 404):
                raise engine_error(exc) from exc

        if session._durability != "persistent" or not remove_workspace:
            return
        try:
            volume = await docker.volumes.get(volume_name(session._workspace_key))
        except DockerError as exc:
            if exc.status == 404:
                return
            raise engine_error(exc) from exc
        try:
            # UNFORCED: the engine's own "volume in use" refusal is the cross-worker
            # reference guard — surface it, never retry with force.
            await volume.delete(force=False)
        except DockerError as exc:
            raise engine_error(exc) from exc

    async def _list_orphan_resources(self) -> list[str]:
        docker = await self._engine()
        try:
            return await self._reconcile_orphans(docker)
        except (OSError, aiohttp.ClientError) as exc:
            raise self._connect_error(exc) from exc

    async def _reconcile_orphans(self, docker: Any) -> list[str]:
        handled: list[str] = []
        containers = await docker.containers.list(all="true", filters=_label_filter())
        for container in containers:
            if container.id in self._ledger:
                continue
            labels = container["Labels"] or {}
            name = self._container_display_name(container)
            if labels.get(LABEL_DURABILITY) == "ephemeral":
                try:
                    await container.delete(force=True, v=True)
                except DockerError as exc:
                    if exc.status != 404:
                        raise engine_error(exc) from exc
                handled.append(f"destroyed orphan ephemeral container {name}")
            else:
                handled.append(f"retained orphan persistent container {name} (adopted on next create)")

        volumes = await docker.volumes.list(filters={"label": [f"{LABEL_SANDBOX}=1"]})
        for volume in volumes.get("Volumes") or []:
            handled.append(f"retained orphan persistent workspace volume {volume['Name']}")
        return handled

    @staticmethod
    def _container_display_name(container: Any) -> str:
        if "Names" in container:
            names = container["Names"]
            if names:
                return str(names[0]).lstrip("/")
        return container.id


# Plain call (not decorator) so ``DockerSandbox`` keeps its concrete class type.
tai42_app.sandboxes.register_sandbox(DockerSandbox)
