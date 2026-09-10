"""Tools contract: the ``ToolInfo`` model + the ``AppTools`` tool/toolkit
registration sub-protocol. Vendor return types (fastmcp ``Tool``, langchain
``StructuredTool``) are ``TYPE_CHECKING``-only.

``AppTools`` is the ``app.tools`` namespace of the assembled facade
(:mod:`tai42_contract.app`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel

from tai42_contract.app.facets import RouteAction
from tai42_contract.manifest import ExtensionElement
from tai42_contract.tools.invocation import (
    ToolInvocation,
    current_tool_invocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)
from tai42_contract.tools.retry import (
    DEFAULT_RETRYABLE_KINDS,
    MAX_ATTEMPTS_CEILING,
    NEVER_RETRYABLE_KINDS,
    ToolRetryBackoff,
    ToolRetryPolicy,
)

if TYPE_CHECKING:
    from fastmcp.tools import Tool
    from langchain_core.tools import StructuredTool

# Preserves the decorated callable's type through ``tool`` / ``toolkit`` so
# ``@app.tools.tool`` keeps the wrapped function's signature instead of ``Any``.
F = TypeVar("F", bound=Callable[..., Any])

#: A base tool's declared tool-references extractor: given a preset's baked
#: ``fixed_kwargs``, returns the tool names a preset of THIS base composes. The
#: platform never knows a base tool's config shape — the base tool declares how to
#: read composed tool names out of its own ``fixed_kwargs``. Every entry must be a
#: string; a non-string / None entry is a plugin bug the reader raises on.
ToolRefsExtractor = Callable[[dict[str, Any]], Iterable[str]]

#: A rename referee: given the tool name about to be renamed, returns
#: human-readable descriptions of every live reference that would be stranded by
#: the rename (empty = no objection). A referee raising is a hard failure of the
#: rename — never a silent bypass. The platform gathers every registered
#: referee's answers and blocks the rename when any is non-empty.
ToolRenameReferee = Callable[[str], Awaitable[list[str]]]

#: A delete referee: given the preset/tool name about to be DELETED, either performs
#: its own CASCADE cleanup of the resources it holds against that name and returns an
#: empty list (allow), or VETOES by returning human-readable descriptions of the live
#: references it will not let the delete strand (non-empty = block). A referee raising is
#: a hard failure of the delete — never a silent bypass. The platform gathers every
#: registered referee's answers and blocks the delete when any is non-empty; a referee
#: that intends to cascade must run its own veto check first, since the platform does not
#: order referees.
ToolDeleteReferee = Callable[[str], Awaitable[list[str]]]

#: A state-template detach referee: given the ``(state name, template name)`` about to be
#: DETACHED, returns human-readable descriptions of every live door binding (a preset
#: version, a conversation config, a hook, a schedule) whose ``state_binding`` still names
#: that template on that state (empty = no objection). A referee raising is a hard failure
#: of the detach — never a silent bypass. The platform gathers every registered referee's
#: answers and blocks the detach when any is non-empty.
StateTemplateDetachReferee = Callable[[str, str], Awaitable[list[str]]]


class ToolInfo(BaseModel):
    """Descriptor for a registered tool.

    ``name`` is the registered key; ``base`` is the underlying tool it was
    bound from (a branch tool names itself, e.g. ``name_chain``).
    """

    name: str
    base: str = ""
    title: str = ""
    description: str = ""


@runtime_checkable
class AppTools(Protocol):
    """Tool + toolkit registration / lookup surface."""

    # Bare ``@app.tools.tool`` decorates the function directly (returns it
    # unchanged); parameterized ``@app.tools.tool(...)`` returns the decorator.
    # Both keep the wrapped function's type. ``tool_refs`` is the optional declared
    # extractor for the composed tool names a preset of this base tool carries in
    # its baked ``fixed_kwargs`` (registered only when the manifest includes the
    # tool). ``retry`` is the optional declared :class:`ToolRetryPolicy` the host
    # dispatch seam retries transient failures of this tool under (likewise
    # registered only when the manifest includes the tool; no declaration = one
    # attempt, exactly). ``tier`` is the optional declared registration tier (a
    # :data:`RouteAction`): ``fenced``/``secret`` gates both AUTHORING a preset over the
    # tool AND RUNNING it (admin-only); ``read``/``write`` carry no execution gate. It is
    # the declarative form of :meth:`register_tier` and, like the two above, registers
    # only when the manifest includes the tool.
    @overload
    def tool(self, func: F, /) -> F: ...
    @overload
    def tool(
        self,
        *args: Any,
        force: bool = False,
        tool_refs: ToolRefsExtractor | None = None,
        retry: ToolRetryPolicy | None = None,
        tier: RouteAction | None = None,
        **kwargs: Any,
    ) -> Callable[[F], F]: ...

    @overload
    def toolkit(self, target: F, /) -> F: ...
    @overload
    def toolkit(self, *args: Any, **kwargs: Any) -> Callable[[F], F]: ...

    def tool_title(self, func: Callable[..., object]) -> str: ...

    async def get_tool(self, key: str) -> Tool: ...

    async def get_tools(self) -> dict[str, Tool]: ...

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]: ...

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any: ...

    def remove_tool(self, name: str) -> None: ...

    # ``combos`` is the tool's list of extension combos (each combo a stack of
    # extension elements — a bare name or a ``{"name", "config"}`` mapping);
    # ``register`` attaches them to the base ``name``.
    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None) -> None: ...

    def unregister_tool_info(self, name: str) -> None: ...

    def unregister_tool_base(self, tool_name: str) -> list[str]: ...

    # The declared tool-references extractor a base tool registered under ``name``,
    # or ``None`` when it declared none — the preset reference collector consults it
    # for a body's ``base_tool``.
    def tool_refs_extractor(self, name: str) -> ToolRefsExtractor | None: ...

    def register_rename_referee(self, provider: ToolRenameReferee) -> None:
        """Register a :data:`ToolRenameReferee` consulted before a tool rename.

        A plugin holding tool-name references calls this through the ``tai42_app``
        handle when its module loads. Every registered referee is asked for the
        old name on a rename; any non-empty answer blocks the rename and its
        descriptions name the holders. Registering the same provider object twice
        raises loudly — a double registration is a plugin bug, never a silent
        duplicate consult."""
        ...

    def register_delete_referee(self, provider: ToolDeleteReferee) -> None:
        """Register a :data:`ToolDeleteReferee` consulted before a preset delete.

        A plugin holding resources keyed on a preset/tool name (e.g. per-node state
        bindings that reference a preset) calls this through the ``tai42_app`` handle when
        its module loads. Every registered referee is asked for the name on a delete; a
        referee cascades its own cleanup and returns empty to allow, or returns non-empty
        descriptions to VETO — any non-empty answer blocks the delete and names the
        holders. Registering the same provider object twice raises loudly — a double
        registration is a plugin bug, never a silent duplicate consult."""
        ...

    def register_detach_referee(self, provider: StateTemplateDetachReferee) -> None:
        """Register a :data:`StateTemplateDetachReferee` consulted before a state-template
        detach.

        A holder of door bindings that name templates (e.g. per-node state bindings, or the
        platform's own preset/route/hook/schedule bindings) calls this through the
        ``tai42_app`` handle when its module loads. Every registered referee is asked for the
        ``(state, template)`` on a detach; any non-empty answer blocks the detach and its
        descriptions name the referencing bindings. Registering the same provider object
        twice raises loudly — a double registration is a bug, never a silent duplicate
        consult."""
        ...

    def register_tier(self, base_tool: str, tier: RouteAction) -> None:
        """Declare ``base_tool``'s registration tier — the authorization character
        (a :data:`RouteAction`) enforced everywhere the tier is consulted.

        A ``fenced`` or ``secret`` tier gates BOTH authoring a preset over the base tool
        (admin-only) AND running the tool: a ``fenced``/``secret`` tool runs only for an
        administrator, at every execution door, and a preset authored over it inherits
        that fence at run time. ``read``/``write`` carry no execution gate. This is the
        programmatic form of ``@app.tools.tool(tier=...)``; both write the one shared
        registry (also read on the authoring side as ``app.presets.registration_tier``).
        One declaration per base tool; a duplicate raises loudly."""
        ...

    def tier(self, base_tool: str) -> RouteAction | None:
        """The registration tier ``base_tool`` declared, or ``None`` when it declared
        none (no execution fence; authoring keeps the presets' default ``write`` action)."""
        ...


__all__ = [
    "DEFAULT_RETRYABLE_KINDS",
    "MAX_ATTEMPTS_CEILING",
    "NEVER_RETRYABLE_KINDS",
    "AppTools",
    "StateTemplateDetachReferee",
    "ToolDeleteReferee",
    "ToolInfo",
    "ToolInvocation",
    "ToolRefsExtractor",
    "ToolRenameReferee",
    "ToolRetryBackoff",
    "ToolRetryPolicy",
    "current_tool_invocation",
    "reset_current_tool_invocation",
    "set_current_tool_invocation",
]
