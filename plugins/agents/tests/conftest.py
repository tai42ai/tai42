"""Bind a recording app to the ``tai42_app`` handle before any test module imports.

Agent modules register through ``tai42_app.agents.agent(name)`` at import time,
mirroring how the host binds the app and then imports the module named by the
manifest's ``agents:`` entry. Binding a recording app here lets a test module
register an agent with the real decorator syntax at module level and then fetch
the live instance back through ``tai42_app.agents.get_agent``.

The recording facets mirror the contract app protocols the agents reach
through the handle:

* ``agents`` (``AppAgents``) — the decorator instantiates the class and stores
  the instance by name; ``get_agent`` raises on an unknown name (never a silent
  ``None``).
* ``monitoring`` (``AppMonitoring``) — ``active`` exposes a stub backend whose
  writer records each ``TraceContext`` and returns a fixed callback list, so the
  run-config helper can be exercised with no live backend.
* ``tools`` (``AppTools``) — ``get_client_tools`` / ``run_tool`` are backed by the
  mutable ``client_tools`` / ``tool_runners`` maps. A test populates the map it
  needs; an unknown name raises a ``RuntimeError`` carrying the name — the
  ``AppTools`` contract prescribes no exception type, so that is the fake's own
  unknown-tool signal. The ``app_tools`` fixture clears both maps per test so
  nothing leaks between tests.
* ``storage`` (``AppStorage``) — ``resource_manager.render_by_id_or_content`` is
  backed by the mutable ``templates`` map with the real manager's semantics: a
  non-``None`` ``content`` renders to itself, a non-``None`` ``template_id`` looks
  up the map, providing both raises ``ValueError``, and an empty call returns
  ``""`` (or raises when ``allow_empty`` is false). An unknown id raises.
  ``resource_manager.normalize_media`` records each call and returns an image
  ``ContentPart`` (a URL passes through, an id becomes a data-URI), raising on a
  resolvable non-image source like the real image-only guard. The
  ``resource_manager`` fixture clears both per test.

Facets a given agent does not use are simply left unconfigured; a test that
needs different behavior monkeypatches the exact seam it calls.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from tai42_contract.agent import Agent
from tai42_contract.app import tai42_app
from tai42_contract.connectors import ResolvedConnectionAuth
from tai42_contract.monitoring import TraceContext
from tai42_contract.sandbox import Sandbox, SandboxPolicy, SandboxUnavailableError
from tests._sandbox_fake import FakeSandbox, make_fake_sandbox, permissive_sandbox_policy


class RecordingAgents:
    """An ``AppAgents`` impl that stores live agent instances by name."""

    def __init__(self) -> None:
        self.registry: dict[str, Agent] = {}
        self.tags: dict[str, set[str]] = {}
        self.meta: dict[str, dict[str, Any]] = {}

    def agent(
        self, name: str, tags: set[str] | None = None, meta: dict[str, Any] | None = None
    ) -> Callable[[type[Agent]], type[Agent]]:
        def decorator(agent_cls: type[Agent]) -> type[Agent]:
            self.registry[name] = agent_cls()
            self.tags[name] = tags or set()
            self.meta[name] = meta or {}
            return agent_cls

        return decorator

    def get_agent(self, name: str) -> Agent:
        agent = self.registry.get(name)
        if agent is None:
            raise RuntimeError(f"No such agent: {name}.")
        return agent


CALLBACKS: list[str] = ["callback-a", "callback-b"]


class RecordingMonitoringWriter:
    """A ``MonitoringWriter`` stub: records each ``TraceContext`` it is asked for
    and hands back a fixed copy of the callback sentinels."""

    def __init__(self) -> None:
        self.contexts: list[TraceContext] = []

    def get_monitoring_callbacks(self, ctx: TraceContext) -> list[str]:
        self.contexts.append(ctx)
        return list(CALLBACKS)


class RecordingMonitoring:
    """A ``Monitoring`` stub exposing the recording writer."""

    def __init__(self) -> None:
        self.writer = RecordingMonitoringWriter()


class RecordingMonitoringFacet:
    """An ``AppMonitoring`` impl whose ``active`` returns the stub backend."""

    def __init__(self) -> None:
        self._backend = RecordingMonitoring()

    @property
    def active(self) -> RecordingMonitoring:
        return self._backend


class RecordingTools:
    """An ``AppTools`` impl backed by mutable maps a test populates.

    ``client_tools`` maps a name to a live ``StructuredTool`` returned by
    ``get_client_tools``; ``tool_runners`` maps a base-tool name to a callable
    invoked by ``run_tool`` (used by preset binding). An unknown name raises a
    ``RuntimeError`` carrying the tool name: the ``AppTools`` contract declares no
    exception type for these calls, so this is the fake's own unknown-tool signal
    and tests assert against it, not against any app-side exception hierarchy.
    """

    def __init__(self) -> None:
        self.client_tools: dict[str, StructuredTool] = {}
        self.tool_runners: dict[str, Callable[..., Any]] = {}
        self.registered_tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        """A no-op tool registrar mirroring ``AppTools.tool``: it records the decorated
        callable by its bound name and returns it unchanged, so an agent module that
        registers a platform tool at import (e.g. the hidden ``agent_resume`` continuation)
        loads cleanly under the recording app. The real binding is exercised by the
        skeleton; here the callable stays directly reachable through
        ``registered_tools``."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            name = kwargs.get("name") or getattr(func, "__name__", repr(func))
            self.registered_tools[name] = func
            return func

        if args and callable(args[0]):
            return decorator(args[0])
        return decorator

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]:
        if names is None:
            return list(self.client_tools.values())
        missing = [name for name in names if name not in self.client_tools]
        if missing:
            raise RuntimeError(f"unknown client tools: {missing}")
        return [self.client_tools[name] for name in names]

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        # ``offload_sync`` mirrors the real facet's keyword-only argument; the fake
        # runs its recorded runners synchronously and has nothing to offload.
        if key not in self.tool_runners:
            raise RuntimeError(f"unknown base tool: {key}")
        result = self.tool_runners[key](**arguments)
        if isinstance(result, Awaitable):
            return await result
        return result


def _looks_like_image(source: str) -> bool:
    """Whether ``source`` names an image, mirroring the real manager's image-only
    guard as closely as a fake can. A resolvable non-``image/*`` suffix is a
    non-image (the real raises); an unresolvable suffix is unknown — the real
    passes such URLs through (the model dereferences) and dereferences storage
    ids to their stored mime — so the fake treats unknown as an image."""
    mime, _ = mimetypes.guess_type(source)
    return mime is None or mime.startswith("image/")


class RecordingResourceManager:
    """A ``resource_manager`` stub for ``render_by_id_or_content`` and
    ``normalize_media``."""

    def __init__(self) -> None:
        self.templates: dict[str, str] = {}
        self.media_calls: list[str | bytes] = []

    async def normalize_media(self, source: str | bytes) -> dict[str, Any]:
        """Record the call and return a model-ready image ``ContentPart``.

        Mirrors the real ``ResourceManager.normalize_media`` branch without any
        network or storage: a public ``http(s)`` URL passes through unchanged; a
        storage id (or raw bytes) resolves to a stand-in base64 ``data:`` URI. So
        an agent using this handle gets a deterministic, image-only content part.
        """
        self.media_calls.append(source)
        if isinstance(source, str) and not _looks_like_image(source):
            raise ValueError(f"normalize_media requires an image resource; resolved non-image mime for {source!r}")
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            return {"type": "image_url", "image_url": {"url": source}}
        return {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}

    async def render_by_id_or_content(
        self,
        content: str | None = None,
        template_id: str | None = None,
        kwargs: dict[str, Any] | None = None,
        allow_empty: bool = True,
    ) -> str:
        if content is not None and template_id is not None:
            raise ValueError("Provide either 'content' OR 'template_id', not both.")
        if content is not None:
            return content
        if template_id is not None:
            if template_id not in self.templates:
                # The real manager raises ``TemplateNotFoundError`` (a bare
                # ``Exception`` subclass tai42-agents cannot import); the agents
                # never catch it, so what a caller observes is "propagates and
                # aborts". ``RuntimeError`` reproduces that without a type — like
                # ``KeyError`` — that a narrow ``except LookupError`` would catch.
                raise RuntimeError(f"unknown template id: {template_id}")
            return self.templates[template_id]
        if allow_empty:
            return ""
        raise ValueError("You must provide either a template or a template_id.")


class RecordingStorage:
    """An ``AppStorage`` impl exposing the recording resource manager."""

    def __init__(self) -> None:
        self.resource_manager = RecordingResourceManager()


class RecordingSandboxes:
    """An ``AppSandboxes`` impl backing the durable-workspace engines' sandbox seam.

    Holds a bound :class:`~tests._sandbox_fake.FakeSandbox` (a real subprocess-backed provider
    over temp workspaces) so ``require_sandbox()`` returns a provider that actually creates
    sessions and ``sandbox_policy()`` returns the permissive resolved policy the shared
    spec-builder reads. A test that exercises the HARD sandbox dependency clears ``provider``
    (or monkeypatches ``require_sandbox``) so the every-door chokepoint raises."""

    def __init__(self) -> None:
        self.provider: FakeSandbox | None = make_fake_sandbox()
        self.policy: SandboxPolicy = permissive_sandbox_policy()

    def register_sandbox(self, cls: type[Sandbox]) -> type[Sandbox]:
        return cls

    @property
    def sandbox(self) -> Sandbox | None:
        return self.provider

    def require_sandbox(self) -> Sandbox:
        if self.provider is None:
            raise SandboxUnavailableError("no sandbox provider is registered (TAI_MCP_SANDBOX / sandbox_module)")
        return self.provider

    def sandbox_policy(self) -> SandboxPolicy:
        return self.policy


class RecordingInteractions:
    """An ``AppInteractions`` facade whose ``ask_user`` records each call and returns a scripted
    answer (sync) — the deep agent reaches the human only through parking tools, so this stays a
    minimal stub the facet needs to be present."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = None

    @property
    def ask_user(self) -> Callable[..., Awaitable[Any]]:
        async def _ask_user(question: Any, **kwargs: Any) -> Any:
            self.calls.append({"question": question, **kwargs})
            return self.answer

        return _ask_user


class RecordingConnectors:
    """An ``AppConnectors`` facade whose ``resolve_connection_auth`` returns a
    per-``connection_id`` :class:`~tai42_contract.connectors.ResolvedConnectionAuth` from a
    mutable map (default: unconfigured → ``None``, injecting nothing). A test that exercises the
    identity-less fail-close sets ``raise_unbound`` so the accessor raises like the real seam."""

    def __init__(self) -> None:
        self.resolved: dict[str, ResolvedConnectionAuth | None] = {}
        self.raise_unbound = False
        self.calls: list[tuple[str, str, str]] = []

    async def resolve_connection_auth(
        self, connection_id: str, provider_id: str, sub_service: str
    ) -> ResolvedConnectionAuth | None:
        self.calls.append((connection_id, provider_id, sub_service))
        if self.raise_unbound:
            raise RuntimeError("resolve_connection_auth: no execution identity is bound (fail-close)")
        return self.resolved.get(connection_id)


class RecordingApp:
    agents = RecordingAgents()
    monitoring = RecordingMonitoringFacet()
    tools = RecordingTools()
    storage = RecordingStorage()
    sandboxes = RecordingSandboxes()
    interactions = RecordingInteractions()
    connectors = RecordingConnectors()


# The durable ``langchain_deep_agent`` reads a REQUIRED digest-pinned ``session_image`` from
# this operator env var; set it once for the whole test process (never read by any other agent)
# so its settings validate whenever a run/astream drive acquires a session. ``setdefault`` leaves
# a real env override in place.
os.environ.setdefault("TAI_AGENTS_LANGCHAIN_DEEP_SESSION_IMAGE", "registry.example/lean@sha256:" + "a" * 64)

# ``claude_code`` and ``langchain_deep_agent`` DECLARE their ``crash_resume`` setting to the
# skeleton at registration (``meta={"tai42/crash_resume": <setting>}``), so their operator
# settings are read as the decorator runs at module import — mirroring the host, which imports an
# agent module only after the operator env is present. Seed the REQUIRED ``claude_code`` model
# credential + digest ``session_image`` so that registration-time read validates in-process; a
# real env override wins via ``setdefault``.
os.environ.setdefault("TAI_AGENTS_CLAUDE_API_KEY", "test-anthropic-key")
os.environ.setdefault("TAI_AGENTS_CLAUDE_SESSION_IMAGE", "registry.example/claude@sha256:" + "c" * 64)

APP = RecordingApp()
tai42_app.bind(APP)


@pytest.fixture(autouse=True)
def _reset_sandbox_facets() -> Iterator[None]:
    """Restore the bound sandbox/connector facets to their defaults around each test, so a test
    that clears the provider or scripts a connection auth never leaks into the next."""
    APP.sandboxes.provider = make_fake_sandbox()
    APP.sandboxes.policy = permissive_sandbox_policy()
    APP.connectors.resolved.clear()
    APP.connectors.raise_unbound = False
    APP.connectors.calls.clear()
    APP.interactions.calls.clear()
    APP.interactions.answer = None
    yield
    provider = APP.sandboxes.provider
    if provider is not None:
        provider.dispose()


@pytest.fixture(autouse=True)
def _route_workspace_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the shared cross-worker workspace lease at a fresh in-memory fakeredis, so a threaded
    run/astream drive serializes on a real SET-NX / compare-and-delete without a live Redis."""
    import contextlib

    from fakeredis import aioredis

    from tai42_agents._internal.park import lease as lease_mod

    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def _fake_lease_client() -> AsyncIterator[Any]:
        yield redis

    monkeypatch.setattr(lease_mod, "_lease_client", _fake_lease_client)


@pytest.fixture
def app_tools() -> Iterator[RecordingTools]:
    """The bound app's ``tools`` facet, cleared before and after each test."""
    APP.tools.client_tools.clear()
    APP.tools.tool_runners.clear()
    yield APP.tools
    APP.tools.client_tools.clear()
    APP.tools.tool_runners.clear()


@pytest.fixture
def resource_manager() -> Iterator[RecordingResourceManager]:
    """The bound app's ``resource_manager``, cleared before and after each test."""
    APP.storage.resource_manager.templates.clear()
    APP.storage.resource_manager.media_calls.clear()
    yield APP.storage.resource_manager
    APP.storage.resource_manager.templates.clear()
    APP.storage.resource_manager.media_calls.clear()
