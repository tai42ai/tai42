"""A local, fully-facetted app for the ``claude_code`` tests.

Deliberately NOT the conftest ``RecordingApp`` (which carries no sandbox / interactions /
connectors facets): ``claude_code`` reaches ``tai42_app.sandboxes.require_sandbox()`` +
``sandbox_policy()``, ``tai42_app.interactions.ask_user``,
``tai42_app.connectors.resolve_connection_auth``, ``tai42_app.tools.run_tool``, the storage
``resource_manager``, and the monitoring writer — so the drive tests bind THIS app for the
block via ``tai42_app.bound(...)``.

The ``_sandbox_fake`` provider runs each session's code as a real host subprocess in a temp
workspace, so a scripted stub runner (``python -m tai_runner``, plain Python speaking the JSONL
protocol — no real SDK) exercises the full exec path deterministically.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.monitoring.models import SpanKind
from tai42_contract.sandbox import Sandbox, SandboxPolicy
from tests._sandbox_fake import FakeSandbox, make_fake_sandbox, permissive_sandbox_policy


class _Span:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def update(self, **kwargs: Any) -> None:
        self._record.update(kwargs)

    @property
    def id(self) -> str:
        return "span"

    def set_trace_metadata(self, **kwargs: Any) -> None:  # pragma: no cover - unused here
        pass


class RecordingWriter:
    """A minimal ``MonitoringWriter`` for the usage-emission test: a settable ``trace_id`` and a
    ``start_span`` that records the span's amended fields (``usage_details``)."""

    def __init__(self) -> None:
        self.trace_id: str | None = None
        self.spans: list[dict[str, Any]] = []
        self.update_current_calls: list[dict[str, Any]] = []

    def current_trace_id(self) -> str | None:
        return self.trace_id

    @contextlib.contextmanager
    def start_span(self, *, name: str, kind: SpanKind, model: str | None = None, **_: Any) -> Any:
        record: dict[str, Any] = {"name": name, "kind": kind, "model": model}
        self.spans.append(record)
        yield _Span(record)

    def update_current_span(self, **kwargs: Any) -> None:  # pragma: no cover - guard the anti-pattern
        self.update_current_calls.append(kwargs)


class _Monitoring:
    def __init__(self, writer: RecordingWriter) -> None:
        self.writer = writer


class _MonitoringFacet:
    def __init__(self, writer: RecordingWriter) -> None:
        self._backend = _Monitoring(writer)

    @property
    def active(self) -> _Monitoring:
        return self._backend


class _SandboxesFacet:
    def __init__(self, sandbox: FakeSandbox, policy: SandboxPolicy) -> None:
        self._sandbox = sandbox
        self._policy = policy

    def register_sandbox(self, cls: type[Sandbox]) -> type[Sandbox]:  # pragma: no cover - unused
        return cls

    @property
    def sandbox(self) -> Sandbox | None:
        return self._sandbox

    def require_sandbox(self) -> Sandbox:
        return self._sandbox

    def sandbox_policy(self) -> SandboxPolicy:
        return self._policy


class _InteractionsFacet:
    def __init__(self, ask_user: Callable[..., Awaitable[Any]]) -> None:
        self._ask_user = ask_user

    @property
    def ask_user(self) -> Callable[..., Awaitable[Any]]:
        return self._ask_user


class _ConnectorsFacet:
    def __init__(self, resolver: Callable[[str, str, str], ResolvedConnectionAuth | None] | None) -> None:
        self._resolver = resolver

    def register_connector(self, descriptor: Any) -> None:  # pragma: no cover - unused
        pass

    @property
    def token_store(self) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def resolve_connection_auth(
        self, connection_id: str, provider_id: str, sub_service: str
    ) -> ResolvedConnectionAuth | None:
        if self._resolver is None:
            raise RuntimeError("no execution identity is bound; connection auth cannot resolve")
        return self._resolver(connection_id, provider_id, sub_service)


class _ToolsFacet:
    def __init__(self, runners: dict[str, Callable[..., Any]]) -> None:
        self.runners = runners
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[kwargs.get("name") or getattr(func, "__name__", repr(func))] = func
            return func

        if args and callable(args[0]):
            return decorator(args[0])
        return decorator

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        if key not in self.runners:
            raise RuntimeError(f"unknown tool: {key}")
        result = self.runners[key](**arguments)
        if isinstance(result, Awaitable):
            return await result
        return result


class _ResourceManager:
    def __init__(self, templates: dict[str, str]) -> None:
        self.templates = templates

    async def list_resources(self) -> list[str]:
        return list(self.templates)

    async def fetch_template(self, key: str) -> str:
        return self.templates[key]


class _StorageFacet:
    def __init__(self, templates: dict[str, str]) -> None:
        self.resource_manager = _ResourceManager(templates)


class LocalApp:
    """A hand-composed app exposing exactly the facets ``claude_code`` reaches at run time."""

    def __init__(
        self,
        *,
        sandbox: FakeSandbox,
        policy: SandboxPolicy,
        ask_user: Callable[..., Awaitable[Any]],
        resolver: Callable[[str, str, str], ResolvedConnectionAuth | None] | None,
        tool_runners: dict[str, Callable[..., Any]],
        templates: dict[str, str],
        writer: RecordingWriter,
    ) -> None:
        self.sandboxes = _SandboxesFacet(sandbox, policy)
        self.interactions = _InteractionsFacet(ask_user)
        self.connectors = _ConnectorsFacet(resolver)
        self.tools = _ToolsFacet(tool_runners)
        self.storage = _StorageFacet(templates)
        self.monitoring = _MonitoringFacet(writer)


async def _default_ask(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - overridden per test
    raise AssertionError("ask_user was not expected in this test")


def build_local_app(
    *,
    sandbox: FakeSandbox | None = None,
    policy: SandboxPolicy | None = None,
    ask_user: Callable[..., Awaitable[Any]] | None = None,
    resolver: Callable[[str, str, str], ResolvedConnectionAuth | None] | None = None,
    tool_runners: dict[str, Callable[..., Any]] | None = None,
    templates: dict[str, str] | None = None,
    writer: RecordingWriter | None = None,
) -> LocalApp:
    return LocalApp(
        sandbox=sandbox or make_fake_sandbox(),
        policy=policy or permissive_sandbox_policy(),
        ask_user=ask_user or _default_ask,
        resolver=resolver,
        tool_runners=tool_runners or {},
        templates=templates or {},
        writer=writer or RecordingWriter(),
    )
