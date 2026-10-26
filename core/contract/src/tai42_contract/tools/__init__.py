"""Tools contract: the ``ToolInfo`` model + the ``AppTools`` tool/toolkit registration sub-protocol.

The vendor return type fastmcp ``Tool`` is ``TYPE_CHECKING``-only; an in-process tool is the
framework-neutral :class:`ClientTool` handle.

``AppTools`` is the ``app.tools`` namespace of the assembled facade (:mod:`tai42_contract.app`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.app.facets import RouteAction
from tai42_contract.errors import ErrorKind
from tai42_contract.manifest import ExtensionElement
from tai42_contract.tools.call_frame import (
    NestedDoorFrameError,
    RunDelivery,
    current_call_chain,
    current_extras,
    get_run_delivery,
    get_run_delivery_id,
    run_delivery,
    tool_call_frame,
)
from tai42_contract.tools.invocation import (
    ToolInvocation,
    current_tool_invocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)
from tai42_contract.tools.meta import (
    TOOL_EXTRA_IDEMPOTENCY_KEY,
    TOOL_META_BADGES,
    TOOL_META_CRASH_RESUME,
    TOOL_META_HIDDEN,
    TOOL_META_PAUSES,
    UndeclaredPauseError,
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

# Preserves the decorated callable's type through ``tool`` / ``toolkit`` so
# ``@app.tools.tool`` keeps the wrapped function's signature instead of ``Any``.
F = TypeVar("F", bound=Callable[..., Any])


class ReferenceHolder(BaseModel):
    """One live holder of a name: what it is, which one, where inside it, and a one-line description.

    ``kind`` is the holder's kind (``"hook"``, ``"preset version"``; a plugin declares its own),
    ``tag`` the feature tag the holder's own doors are registered under (the tag a role's grant
    map names), ``name`` the holder's own name, ``locator`` the fields that place the reference
    inside the holder (a version, a node id), and ``summary`` the line a person reads. A caller
    whose role may not read ``tag`` reads the holders of one kind as a single count holder:
    ``kind`` and ``tag`` as above, ``name`` the tag, ``locator`` only ``count``, and a ``summary``
    that names no holder.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    name: str = Field(min_length=1)
    locator: dict[str, str | int] = Field(default_factory=dict)
    summary: str = Field(min_length=1)


class ComposedToolRef(BaseModel):
    """One tool name a kwargs blob composes, and where the base tool's own config names it.

    ``site`` is the base tool's own words for where the name sits (``None`` when the place has no
    inner position); ``tool_kwargs`` is the kwargs that place passes to the composed tool, as
    stored (``None`` when the config passes none).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    site: str | None = Field(default=None, min_length=1)
    tool_kwargs: dict[str, Any] | None = None


#: A base tool's declared tool-references extractor. It is called with a kwargs blob the base
#: tool receives — a preset body's ``fixed_kwargs``, a hook's or schedule's ``tool_kwargs``, an
#: inline preset's ``fixed_kwargs``, the ``tool_kwargs`` a composed site passes — as given, before
#: any validation of its own. It returns one :class:`ComposedToolRef` per place the blob names a
#: tool, and none when the blob names no tool (its config key absent or ``None``, or a form the
#: base resolves only at run). ``site`` is printed verbatim after the quoted name in a refusal
#: (``at <site>``), so it is a short phrase in the base tool's own vocabulary; the platform never
#: parses it. ``tool_kwargs`` is walked by the platform with the same function; the extractor
#: never interprets it. A blob off the shape the base accepts is answered by raising
#: ``ValueError`` whose text names the place and the fault and quotes no value from the blob;
#: any other exception, or an entry that is not a ``ComposedToolRef``, is a plugin bug answered
#: as a server fault naming the base tool.
ToolRefsExtractor = Callable[[dict[str, Any]], Iterable[ComposedToolRef]]


class TemplateRef(BaseModel):
    """One stored template id a kwargs blob names, and where the base tool's own config names it.

    ``site`` is the base tool's own words for the place, printed as given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    site: str = Field(min_length=1)


#: A base tool's declared template-references extractor, with the calling contract of
#: :data:`ToolRefsExtractor`: called with a kwargs blob the base tool receives, as given, it
#: returns one :class:`TemplateRef` per place the blob names a stored template id, and none
#: when it names none. A blob off the shape the base accepts is answered by raising
#: ``ValueError`` whose text names the place and quotes no value from the blob; any other
#: exception, or an entry that is not a ``TemplateRef``, is a plugin bug answered as a server
#: fault naming the base tool.
TemplateRefsExtractor = Callable[[dict[str, Any]], Iterable[TemplateRef]]

#: A rename referee: given the set of tool names a rename moves — the renamed tool and every
#: extension branch tool bound off it, or every tool one check moves at once — answers, keyed
#: by asked name, one :class:`ReferenceHolder` per live reference the rename would strand. A
#: name it holds nothing on is left out or maps to an empty list (no objection); a key that was
#: not asked is the referee's bug and fails the rename. It answers holders and has no side
#: effect: the rename preview calls it. It is asked once per gather, for all of those names
#: together. A referee raising is a hard failure of the rename — never a silent bypass. The
#: platform gathers every registered referee's answers and blocks the rename when any holds.
ToolRenameReferee = Callable[[frozenset[str]], Awaitable[dict[str, list[ReferenceHolder]]]]

#: A delete referee: given the set of tool names a delete is about to remove — the preset and
#: every extension branch tool bound off it — answers, keyed by asked name, one
#: :class:`ReferenceHolder` per live reference it will not let the delete strand (nothing held
#: = allow, any holder = VETO). A name it holds nothing on is left out or maps to an empty list;
#: a key that was not asked is the referee's bug and fails the delete. It answers holders and
#: has no side effect: the delete preview calls it. It is asked once per gather, for all of
#: those names together, and a holder's summary names the name it holds. Cleanup belongs in a
#: :data:`ToolDeleteCascade`. A referee raising is a hard failure of the delete — never a
#: silent bypass. The platform gathers every registered referee's answers and blocks the
#: delete when any holds.
ToolDeleteReferee = Callable[[frozenset[str]], Awaitable[dict[str, list[ReferenceHolder]]]]

#: A delete cascade: given a tool name a delete removes, cleans up what its owner holds
#: against that name. The platform calls it once for each tool the delete removes (the
#: preset, then each extension branch tool), only after every veto and guard has passed and
#: before the record is removed. A cascade raising fails the delete loudly. The platform does
#: not order the cascades among themselves.
ToolDeleteCascade = Callable[[str], Awaitable[None]]

#: A state-template detach referee: given the ``(state name, template name)`` about to be
#: DETACHED, returns one :class:`ReferenceHolder` per live door binding (a preset version, a
#: conversation config, a hook, a schedule) whose ``state_binding`` still names that template
#: on that state (empty = no objection). It answers holders and has no side effect: the detach
#: preview calls it. A referee raising is a hard failure of the detach — never a silent
#: bypass. The platform gathers every registered referee's answers and blocks the detach when
#: any is non-empty.
StateTemplateDetachReferee = Callable[[str, str], Awaitable[list[ReferenceHolder]]]

#: A stored-template delete referee: given the set of ids a delete would remove, returns one
#: :class:`ReferenceHolder` per place a stored object its owner holds names one of them (empty
#: = allow, non-empty = VETO). It changes nothing. A referee raising is a hard failure of the
#: delete — never a silent bypass.
TemplateDeleteReferee = Callable[[frozenset[str]], Awaitable[list[ReferenceHolder]]]


@runtime_checkable
class ClientTool(Protocol):
    """A registered tool bound for in-process use by an agent engine, framework-neutral.

    ``name`` is the full registered name (never truncated); ``input_schema`` is the JSON Schema of
    the arguments, the schema the MCP edge advertises. ``invoke`` returns the tool's value with
    secrets masked, or a ``SuspendedInteraction`` / ``ResumeBuffered`` (by type, unadapted) when a
    tool that declares it can pause parked; it raises :class:`ClientToolError` for a failure the
    model should see (the body raised, a driver reached a FAILED terminal, the result cannot be
    JSON-encoded, the arguments do not validate), and raises every other error raw (tier refusal,
    authorization, unknown or vanished tool, a park signal from a tool that does not declare it can
    pause — :class:`UndeclaredPauseError` — cancellation).
    """

    @property
    def name(self) -> str:
        """The full registered tool name."""
        ...

    @property
    def description(self) -> str:
        """The tool's description, as the model reads it."""
        ...

    @property
    def input_schema(self) -> dict[str, Any]:
        """The JSON Schema of the tool's arguments."""
        ...

    async def invoke(self, arguments: Mapping[str, Any]) -> Any:
        """Run the tool once with ``arguments``."""
        ...


class ClientToolError(Exception):
    """One call's model-visible failure; the message is worded for the model and names the tool."""

    def __init__(self, tool_name: str, message: str) -> None:
        """Carry the failed tool's name beside the model-facing ``message``."""
        super().__init__(message)
        self.tool_name = tool_name


class ToolAlreadyRegisteredError(ValueError):
    """A tool of this name is already bound on the live tool surface."""

    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, name: str) -> None:
        """Name the tool whose name is taken."""
        super().__init__(f"tool {name!r} is already registered")
        self.name = name


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
    # :data:`ToolRefsExtractor`: called with any kwargs blob this base tool receives,
    # unvalidated, it returns one :class:`ComposedToolRef` per place the blob names a
    # tool (none when it names none), raises ``ValueError`` with a value-free text for
    # an off-shape blob, and any other exception is a plugin bug (registered only when
    # the manifest includes the tool). ``template_refs`` is the optional declared
    # :data:`TemplateRefsExtractor`, with the same calling contract, answering one
    # :class:`TemplateRef` per stored template id the blob names (likewise registered only
    # when the manifest includes the tool). ``retry`` is the optional declared :class:`ToolRetryPolicy` the host
    # dispatch seam retries transient failures of this tool under (likewise
    # registered only when the manifest includes the tool; no declaration = one
    # attempt, exactly). ``tier`` is the optional declared registration tier (a
    # :data:`RouteAction`): ``fenced``/``secret`` gates both AUTHORING a preset over the
    # tool AND RUNNING it (admin-only); ``read``/``write`` carry no execution gate. It is
    # the declarative form of :meth:`register_tier` and, like the two above, registers
    # only when the manifest includes the tool. ``extras_keys`` declares the door
    # ``extras`` keys this tool reads through :meth:`extras`; a key a target does not
    # declare is refused before the run, and a preset inherits its base tool's keys
    # (likewise registered only when the manifest includes the tool). A tool that can
    # pause declares ``meta={TOOL_META_PAUSES: True}`` (see :meth:`pauses`). A registration
    # raises :class:`ToolAlreadyRegisteredError` when a tool of that name is already bound.
    @overload
    def tool(self, func: F, /) -> F: ...
    @overload
    def tool(
        self,
        *args: Any,
        force: bool = False,
        tool_refs: ToolRefsExtractor | None = None,
        template_refs: TemplateRefsExtractor | None = None,
        retry: ToolRetryPolicy | None = None,
        tier: RouteAction | None = None,
        extras_keys: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[F], F]: ...

    @overload
    def toolkit(self, target: F, /) -> F: ...
    @overload
    def toolkit(self, *args: Any, **kwargs: Any) -> Callable[[F], F]: ...

    def tool_title(self, func: Callable[..., object]) -> str:
        """Return the human-facing title of the tool implemented by ``func``."""
        ...

    async def get_tool(self, key: str) -> Tool:
        """Return the assembled tool registered under ``key``."""
        ...

    async def has_tool(self, key: str) -> bool:
        """Whether a tool is registered under ``key``, answered by a lookup of that one name."""
        ...

    async def get_tools(self) -> dict[str, Tool]:
        """Return every assembled tool keyed by name."""
        ...

    async def get_client_tools(self, names: list[str] | None = None) -> list[ClientTool]:
        """The tools named in ``names`` (all when ``None``) as framework-neutral handles; an unknown name raises."""
        ...

    async def run_tool(
        self,
        key: str,
        arguments: dict[str, Any],
        *,
        offload_sync: bool = False,
        continues_chain: Sequence[str] | None = None,
        extras: Mapping[str, Any] | None = None,
    ) -> Any:
        """Execute the tool registered under ``key`` with ``arguments`` and return its result.

        ``offload_sync`` runs a synchronous tool body off the event loop in a worker thread.

        ``continues_chain`` is an in-process seam keyword ONLY (no request model, MCP
        argument, or tool argument sets it): when given, the dispatch's call frame SETS
        the ambient call chain to it rather than pushing ``key``, so a continuation
        runner restores a parked run's chain on the one dispatch that resumes it.

        ``extras`` is likewise an in-process seam keyword ONLY (no request model, MCP
        argument, or tool argument sets it): the mapping a door carries into the run it
        starts. It is set on the dispatch's call frame — ambient and read-only for THAT
        frame, so the started tool reads it through :meth:`extras` and every nested frame
        starts empty. A visit checks the keys against the target's declaration before the
        run; ``None`` binds an empty mapping.
        """
        ...

    def extras(self) -> Mapping[str, Any]:
        """The ambient door ``extras`` mapping the current run was started with (empty when none).

        A door carries author-configured ``extras`` into a run; a tool reads the keys it declared
        (``@app.tools.tool(extras_keys=...)``) here. The mapping is read-only and scoped to the
        started target's own frame — every nested dispatch reads an empty mapping, so nothing leaks
        down. No request model, MCP argument, or tool argument can set it.
        """
        ...

    async def declared_extras(self, name: str) -> frozenset[str]:
        """The door ``extras`` keys tool ``name`` is declared to read (empty when it declares none).

        A preset inherits its base tool's declared keys. The visit checks a door's ``extras`` against
        this set before starting the target and refuses an undeclared key.
        """
        ...

    async def pauses(self, key: str) -> bool:
        """Whether a dispatch of ``key`` can return a park signal.

        The tool registered under ``key`` declares ``TOOL_META_PAUSES``, or it is an extension
        branch whose base declares it or whose extension stack holds an extension registered
        with ``pauses=True``, or it is a preset (transformed tool) whose ``parent_tool`` chain
        reaches such a tool. An unknown name answers ``False`` (its own dispatch surfaces the
        not-found). A dispatch of a tool that answers ``False`` and returns a park signal is
        refused with :class:`UndeclaredPauseError`.
        """
        ...

    def remove_tool(self, name: str) -> None:
        """Remove the tool registered under ``name``."""
        ...

    def surface_generation(self) -> int:
        """A number that changes whenever the set of tools this process lists changes.

        Monotonic within a process; never compared across processes. A consumer that caches
        anything derived from ``get_tools``/``get_client_tools`` keys the cache on it.
        """
        ...

    # ``combos`` is the tool's list of extension combos (each combo a stack of
    # extension elements — a bare name or a ``{"name", "config"}`` mapping);
    # ``register`` attaches them to the base ``name``.
    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None) -> None:
        """Register base tool ``name``'s extension combos, attaching each combo stack to the base."""
        ...

    def unregister_tool_info(self, name: str) -> None:
        """Drop the registered tool-info for ``name``."""
        ...

    def unregister_tool_base(self, tool_name: str) -> list[str]:
        """Unregister base tool ``tool_name`` and return the names removed."""
        ...

    # The declared tool-references extractor a base tool registered under ``name``,
    # or ``None`` when it declared none — the preset reference collector consults it
    # for a body's ``base_tool``.
    def tool_refs_extractor(self, name: str) -> ToolRefsExtractor | None:
        """Return the tool-references extractor base tool ``name`` registered, or ``None`` when it declared none."""
        ...

    def template_refs_extractor(self, name: str) -> TemplateRefsExtractor | None:
        """Return the template-references extractor base tool ``name`` registered, or ``None`` when it declared none."""
        ...

    async def kwargs_template_refs(self, tool: str, kwargs: Mapping[str, Any]) -> list[TemplateRef]:
        """The stored template ids a call of ``tool`` with ``kwargs`` names, one ``TemplateRef`` per id.

        ``site`` is the place inside ``kwargs`` (``system_prompt``, ``subagents[0].system_prompt``), in the
        walk's order; ``[]`` when the blob names none. A tool the registry does not serve names nothing:
        ``[]``, as the platform's delete referees read a stored call of it; the call itself fails where it
        runs. Reads the ids with the same walk the platform's write check and delete referees run. Raises
        ``ValueError`` naming the place, quoting no value, for a blob the tool's input refuses, and
        ``RuntimeError`` for a fault of a declared ``tool_refs`` or ``template_refs`` extractor.
        """
        ...

    def register_rename_referee(self, provider: ToolRenameReferee) -> None:
        """Register a :data:`ToolRenameReferee` consulted before a tool rename.

        A plugin holding tool-name references calls this through the ``tai42_app``
        handle when its module loads. Every registered referee is asked once, with the
        old name and every extension branch tool bound off it together, on a rename and
        on the rename preview; it answers holders keyed by asked name and has no side
        effect. Any holder blocks the rename and the holders are named. Registering
        the same provider object twice raises loudly — a double registration is a
        plugin bug, never a silent duplicate consult.
        """
        ...

    def register_delete_referee(self, provider: ToolDeleteReferee) -> None:
        """Register a :data:`ToolDeleteReferee` consulted before a preset delete.

        A plugin holding resources keyed on a preset/tool name calls this through the
        ``tai42_app`` handle when its module loads. Every registered referee is asked once,
        with every tool the delete removes together — the preset and every extension branch
        tool bound off it — on a delete and on the delete preview; it answers holders keyed
        by asked name and has no side effect. Any holder VETOES the delete and the holders
        are named. Cleanup belongs in a delete cascade (:meth:`register_delete_cascade`).
        Registering the same provider object twice raises loudly — a double registration is
        a plugin bug, never a silent duplicate consult.
        """
        ...

    def register_delete_cascade(self, provider: ToolDeleteCascade) -> None:
        """Register a :data:`ToolDeleteCascade` run when a preset delete proceeds.

        A plugin holding resources keyed on a preset/tool name calls this through the
        ``tai42_app`` handle when its module loads. A cascade is called once for each tool
        the delete removes (the preset, then each extension branch tool bound off it), with
        that tool's name, only after every veto and guard has passed and before the record
        is removed; a cascade raising fails the delete. Registering the same provider object
        twice raises loudly — a double registration is a plugin bug, never a silent
        duplicate cleanup.
        """
        ...

    def delete_cascades(self) -> list[ToolDeleteCascade]:
        """Every registered :data:`ToolDeleteCascade`, in registration order."""
        ...

    def register_detach_referee(self, provider: StateTemplateDetachReferee) -> None:
        """Register a :data:`StateTemplateDetachReferee` consulted before a state-template detach.

        A holder of door bindings that name templates (e.g. per-node state bindings, or the
        platform's own preset/route/hook/schedule bindings) calls this through the
        ``tai42_app`` handle when its module loads. Every registered referee is asked for the
        ``(state, template)`` on a detach and on the detach preview; it answers holders and
        has no side effect. Any non-empty answer blocks the detach and the holders are named.
        Registering the same provider object twice raises loudly — a double registration is a
        bug, never a silent duplicate consult.
        """
        ...

    def register_tier(self, base_tool: str, tier: RouteAction) -> None:
        """Declare ``base_tool``'s registration tier (a :data:`RouteAction`).

        The authorization character enforced everywhere the tier is consulted.

        A ``fenced`` or ``secret`` tier gates BOTH authoring a preset over the base tool
        (admin-only) AND running the tool: a ``fenced``/``secret`` tool runs only for an
        administrator, at every execution door, and a preset authored over it inherits
        that fence at run time. ``read``/``write`` carry no execution gate. This is the
        programmatic form of ``@app.tools.tool(tier=...)``; both write the one shared
        registry (also read on the authoring side as ``app.presets.registration_tier``).
        One declaration per base tool; a duplicate raises loudly.
        """
        ...

    def tier(self, base_tool: str) -> RouteAction | None:
        """The registration tier ``base_tool`` declared, or ``None`` when it declared none.

        None means no execution fence; authoring keeps the presets' default ``write`` action.
        """
        ...


__all__ = [
    "DEFAULT_RETRYABLE_KINDS",
    "MAX_ATTEMPTS_CEILING",
    "NEVER_RETRYABLE_KINDS",
    "TOOL_EXTRA_IDEMPOTENCY_KEY",
    "TOOL_META_BADGES",
    "TOOL_META_CRASH_RESUME",
    "TOOL_META_HIDDEN",
    "TOOL_META_PAUSES",
    "AppTools",
    "ClientTool",
    "ClientToolError",
    "ComposedToolRef",
    "NestedDoorFrameError",
    "ReferenceHolder",
    "RunDelivery",
    "StateTemplateDetachReferee",
    "TemplateDeleteReferee",
    "TemplateRef",
    "TemplateRefsExtractor",
    "ToolAlreadyRegisteredError",
    "ToolDeleteCascade",
    "ToolDeleteReferee",
    "ToolInfo",
    "ToolInvocation",
    "ToolRefsExtractor",
    "ToolRenameReferee",
    "ToolRetryBackoff",
    "ToolRetryPolicy",
    "UndeclaredPauseError",
    "current_call_chain",
    "current_extras",
    "current_tool_invocation",
    "get_run_delivery",
    "get_run_delivery_id",
    "reset_current_tool_invocation",
    "run_delivery",
    "set_current_tool_invocation",
    "tool_call_frame",
]
