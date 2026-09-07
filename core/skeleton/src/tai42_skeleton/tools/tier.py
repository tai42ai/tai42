"""The per-tool registration-tier registry and the execution-time fence it powers.

A tool's registration tier (:data:`~tai42_contract.app.RouteAction` —
``read``/``write``/``fenced``/``secret``) is declared once and enforced everywhere. It is
declared programmatically through ``app.tools.register_tier`` (surfaced on the authoring
side under its existing ``app.presets.register_registration_tier`` name — the SAME
registry, no second copy) or declaratively via ``@app.tools.tool(tier=...)``. It gates
two things: AUTHORING a preset over the tool (the presets chokepoint,
``operations/presets.py``) and RUNNING the tool (this fence, at every execution door).

A ``fenced`` or ``secret`` tool runs only for an administrator; ``read``/``write`` tools
carry no execution gate (the per-role write level keeps governing them). A preset keys on
its BASE tool, so a preset authored over a fenced tool inherits the fence at run time.
The fence is built through ONE shared function (:func:`enforce_run_tier`) called at each
seam a dispatch can reach the tool BODY through, so no door carries its own copy: the
in-process :meth:`~tai42_skeleton.tools.binding.ToolBinding.run_tool` seam, the agent
tool-dispatch door (:meth:`~tai42_skeleton.tools.binding.ToolBinding._client_runnable`,
the langchain client tool an in-process agent invokes, which reaches the body directly),
and the MCP tool-call edge (which reaches ``Tool.run`` directly through the FastMCP
middleware chain).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tai42_contract.app import RouteAction

    from tai42_skeleton.app.server import TaiMCP


class ToolTierRegistry:
    """The one process-shared map ``base_tool -> RouteAction``, reset on every
    ``start()`` so a reload re-imports the tool modules and re-registers cleanly.

    A duplicate registration for a base tool raises loudly: a silent overwrite could swap
    a tool's authorization character out from under both the authoring gate and the run
    fence."""

    def __init__(self) -> None:
        self._tiers: dict[str, RouteAction] = {}

    def register(self, base_tool: str, tier: RouteAction) -> None:
        if base_tool in self._tiers:
            raise ValueError(f"registration tier for base tool {base_tool!r} is already registered")
        self._tiers[base_tool] = tier

    def get(self, base_tool: str) -> RouteAction | None:
        return self._tiers.get(base_tool)

    def reset(self) -> None:
        self._tiers.clear()


def resolve_run_tier(app: TaiMCP, key: str) -> RouteAction | None:
    """The registration tier governing a RUN of ``key``, or ``None`` when none is declared.

    A preset keys on its base tool, so a registered preset resolves to
    ``spec.base_tool`` first; an extension BRANCH resolves to its origin base
    (``base_of``). The tier is then read for that base tool — a preset over a fenced base,
    and a branch of one, both inherit the fence."""
    manager = app.preset_manager
    base = manager.get_spec(key).base_tool if manager.is_registered(key) else key
    base = app.tools.base_of(base)
    return app.tools.tier(base)


async def enforce_run_tier(app: TaiMCP, key: str) -> None:
    """Fence a ``fenced``/``secret`` tool at execution: the acting principal must be an
    administrator, else refuse LOUDLY with the platform's ``ForbiddenError`` naming the
    tool and the tier.

    A non-fenced tool never resolves a caller, so this is a dict read on the hot path.
    ``resolve_caller`` returns an admin with access control off, so the fence bites only
    where the platform fences at all; a background/hook/schedule fire resolves its bound
    execution identity, so the refusal there is a recorded failed run, never a silent
    no-op."""
    tier = resolve_run_tier(app, key)
    if tier not in ("fenced", "secret"):
        return
    from tai42_skeleton.operations._authority import resolve_caller
    from tai42_skeleton.operations.errors import ForbiddenError

    caller = await resolve_caller()
    if not caller.is_admin:
        raise ForbiddenError(f"tool {key!r} is {tier} and may be run only by an administrator")


class ToolTierFenceMiddleware(Middleware):
    """Enforce the run-time tier fence at the MCP session tool-call edge.

    An MCP ``tools/call`` reaches ``Tool.run`` through the FastMCP middleware chain, never
    the ``ToolBinding.run_tool`` seam that fences the in-process doors, so this edge
    enforces the same fence itself. A denial raises a :class:`~fastmcp.exceptions.ToolError`
    backed by the platform's ``ForbiddenError``. Installed on the main server and every
    sub-MCP mount (mirroring ``AuthzMiddleware``) and added before the turn-budget
    middleware so a fenced denial never opens a budget window."""

    def __init__(self, app: TaiMCP) -> None:
        self._app = app

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[Any]],
    ) -> Any:
        from fastmcp.exceptions import ToolError

        from tai42_skeleton.operations.errors import ForbiddenError

        try:
            await enforce_run_tier(self._app, context.message.name)
        except ForbiddenError as exc:
            raise ToolError(str(exc)) from exc
        return await call_next(context)
