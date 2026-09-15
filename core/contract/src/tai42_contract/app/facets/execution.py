"""Compute/registration provider facets: agents, backends, sandboxes, storage, extensions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from tai42_contract.agent import Agent
from tai42_contract.backend import Backend
from tai42_contract.extensions import ExtensionKind
from tai42_contract.sandbox import Sandbox, SandboxPolicy
from tai42_contract.storage import Storage

_AgentT = TypeVar("_AgentT", bound=Agent)
_StorageT = TypeVar("_StorageT", bound=Storage)


@runtime_checkable
class AppAgents(Protocol):
    """The agent-provider namespace (``app.agents``)."""

    def agent(
        self, name: str, tags: set[str] | None = None, meta: dict[str, Any] | None = None
    ) -> Callable[[type[_AgentT]], type[_AgentT]]:
        """Register an :class:`Agent` subclass under ``name`` and auto-register its JSON ``run`` tool.

        ``tags`` are the run tool's native tags, set on its constructed tool object.
        ``meta`` is generic registration metadata threaded onto the same constructed
        run-tool object (naming no consumer concept — a registrant attaches any generic
        ``tai42/*`` key, e.g. a crash-resume flag the run-dispatch seam reads). The
        decorator returns the class unchanged, so the decorated symbol keeps its
        concrete subclass type.
        """
        ...

    def get_agent(self, name: str) -> Agent:
        """Fetch a registered agent instance by name; raise if missing."""
        ...

    def all_agents(self) -> dict[str, Agent]:
        """Return every registered agent keyed by registration name.

        The result is a shallow copy, so a caller iterating it cannot mutate the live registry. The preset
        bind kernel reads the keys to detect an agent base (the run tool binds under the registration name).
        """
        ...


@runtime_checkable
class AppBackends(Protocol):
    """The backend-provider namespace (``app.backends``)."""

    def register_backend(
        self, cls: type[Backend] | None = None
    ) -> Callable[[type[Backend]], type[Backend]] | type[Backend]:
        """Register the :class:`Backend` provider; usable bare or as a decorator factory."""
        ...

    @property
    def backend(self) -> Backend | None:
        """The registered backend provider, or ``None`` when none is registered."""
        ...


@runtime_checkable
class AppSandboxes(Protocol):
    """The sandbox-provider namespace (``app.sandboxes``)."""

    def register_sandbox(self, cls: type[Sandbox]) -> type[Sandbox]:
        """Register the :class:`Sandbox` provider (decorator form, returning the class unchanged).

        One provider backs the slot; a second registration is a conflict the runtime rejects loudly.
        """
        ...

    @property
    def sandbox(self) -> Sandbox | None:
        """The registered provider, or ``None`` — status/introspection ONLY.

        Never gate execution on this nullable read; acquire through :meth:`require_sandbox`, the every-door
        guarantee (a ``None`` property is how per-consumer checks drift).
        """
        ...

    def require_sandbox(self) -> Sandbox:
        """Return the registered provider from the ONE raising acquisition chokepoint.

        Raise :class:`~tai42_contract.sandbox.SandboxUnavailableError` (naming ``TAI_MCP_SANDBOX`` and
        ``sandbox_module``) when none is registered. EVERY consumer acquires through here.
        """
        ...

    def sandbox_policy(self) -> SandboxPolicy:
        """Return the skeleton-resolved :class:`~tai42_contract.sandbox.SandboxPolicy`.

        This is the plugin-reachable READ of the SAME policy the skeleton binds to the kit at the
        session-create chokepoint.

        Available REGARDLESS of whether a provider is registered (it reads operator
        config, not a provider). Mirrors the ``ask_user`` / ``resolve_connection_auth``
        facade accessors that let an in-process plugin read a skeleton-resolved value
        without importing the skeleton; a consumer building a policied spec reads the
        network default and ``scrub_transcript`` here. Implemented in the skeleton,
        which resolves the policy once and returns the SAME value it binds to the kit.
        """
        ...


@runtime_checkable
class AppStorage(Protocol):
    """The storage-provider namespace (``app.storage``)."""

    @overload
    def register_storage(self, cls: type[_StorageT]) -> type[_StorageT]: ...
    @overload
    def register_storage(self, cls: None = None) -> Callable[[type[Storage]], type[Storage]]: ...
    def register_storage(
        self, cls: type[Storage] | None = None
    ) -> Callable[[type[Storage]], type[Storage]] | type[Storage]:
        """Register the :class:`Storage` provider; usable bare or as a decorator factory."""
        ...

    @property
    def resource_manager(self) -> Any:
        """The active resource manager (impl type; loads/renders content over storage)."""
        ...


@runtime_checkable
class AppExtensions(Protocol):
    """The tool-extension provider namespace (``app.extensions``)."""

    def extension(
        self,
        f: Callable[..., Any] | None = None,
        *,
        kind: ExtensionKind,
        name: str | None = None,
        requires_body_locality: bool = False,
    ) -> Callable[..., Any]:
        """Register a tool-extension factory under ``name`` (default: the function's own name).

        Usable bare or with arguments. ``requires_body_locality`` marks an extension whose wrapper only works
        in the process running the tool body it wraps — e.g. a proxy layer that
        routes the body's egress through a task-scoped contextvar, visible only
        where the body executes. The flag is stored as registration metadata the
        apply site reads to order a stacked combo: a locality-requiring
        extension must bind INSIDE any execution-relocating extension
        (``ExtensionKind.relocates_execution``), so its wrapper travels with the
        body to the worker. Bound outside a relocating layer, the wrapper stays
        behind in the submitting process and silently does not apply.
        """
        ...

    def available_extensions(self) -> list[dict[str, str]]:
        """List registered extension modules as ``[{"name", "kind"}]``."""
        ...
