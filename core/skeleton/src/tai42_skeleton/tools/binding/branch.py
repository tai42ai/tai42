"""Expands a base tool into its extension-combo branches and registers each resulting tool onto the FastMCP server."""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool_transform import TransformedTool
from tai42_contract.tools import TOOL_META_PAUSES

from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.extensions.registry import extension_config, extension_name, factory_accepts_config
from tai42_skeleton.tools.binding.baked_partial import _cached_baked_partial
from tai42_skeleton.tools.binding.extension_schema import _enforce_extension_schema
from tai42_skeleton.tools.binding.schema import _declares_own_output_schema, _derive_output_schema
from tai42_skeleton.tools.binding.secret_tool import _SecretRevealingTool
from tai42_skeleton.tools.binding.state import _ToolBindingBase
from tai42_skeleton.tools.binding.surface import add_surface_tool


@dataclass(frozen=True)
class _Branch:
    """One branch tool an expansion binds: its callable (or prebuilt tool), description and stack facts.

    ``preserves_output`` — every extension in its stack preserves the base's output shape;
    ``stack_pauses`` — an extension in its stack is registered ``pauses=True``.
    """

    func: Callable[..., Any] | Tool
    description: str | None
    preserves_output: bool
    stack_pauses: bool


def _declares_pauses(meta: dict[str, Any] | None) -> bool:
    return (meta or {}).get(TOOL_META_PAUSES) is True


@dataclass(frozen=True)
class _BranchBase:
    """The resolved base a branch expansion builds from.

    The original registered object/callable, the typed callable extensions wrap, its inherited docstring,
    name, description, and derived output schema.
    """

    func: Callable[..., Any] | Tool
    is_obj: bool
    branch_base: Callable[..., Any]
    base_doc: str | None
    orig_name: str
    orig_desc: str | None
    base_output_schema: dict[str, Any] | None


class _BranchBindingMixin(_ToolBindingBase):
    """Builds the extension-combo branch set for a base tool and binds each resulting tool onto the live server."""

    def bind_tool_func(self, *args, owner: str | None = None, **kwargs):
        def bind(func):
            # ``func`` is either a callable (the ordinary decorator path) or a
            # prebuilt ``Tool`` object (a preset's baked transform). A Tool object
            # is registered DIRECTLY for its bare name (preserving its typed
            # schema), and its BRANCHES wrap a reconstructed typed callable — a
            # transformed tool exposes no plain callable of its own.
            prepared = self._prepared_branch(func, kwargs)
            extend_tools = self._expanded_tools(prepared)
            self._register_expanded(prepared, extend_tools, args, kwargs, owner)
            return func

        return bind

    def _prepared_branch(self, func: Callable[..., Any] | Tool, kwargs: dict[str, Any]) -> _BranchBase:
        """Resolve the branch base for ``func``.

        Yields the typed callable extensions wrap, the inherited docstring, name, description, and derived output
        schema.

        Pops ``name``/``description`` off ``kwargs`` so each reaches ``bind_tool`` exactly
        once (as the carried value), never a second time through ``**kwargs``.
        """
        is_obj = isinstance(func, Tool)
        branch_base = self._branch_base_callable(func) if is_obj else func
        # The docstring an extension inherits from the layer it wraps: the
        # branch base callable's, NOT the ``Tool`` object's (a Tool object's
        # ``__doc__`` is the fastmcp CLASS docstring, not the tool
        # description). A wrapper that leaves the docstring at this inherited
        # value has not authored its own, so the running description survives.
        base_doc = branch_base.__doc__ if is_obj else func.__doc__

        orig_name = kwargs.pop("name", None) or (func.name if is_obj else func.__name__)
        orig_desc = kwargs.pop("description", None) or (func.description if is_obj else inspect.getdoc(func))

        # The base tool's declared OUTPUT schema, derived from the branch base
        # callable (the base is bound AFTER its branches, so it is not yet a
        # registered Tool to read from). A shape-preserving branch that
        # declares no output schema of its own inherits this, so the
        # structured-output contract survives the wrap.
        base_output_schema = _derive_output_schema(branch_base)
        return _BranchBase(
            func=func,
            is_obj=is_obj,
            branch_base=branch_base,
            base_doc=base_doc,
            orig_name=orig_name,
            orig_desc=orig_desc,
            base_output_schema=base_output_schema,
        )

    def _expanded_tools(self, prepared: _BranchBase) -> dict[str, _Branch]:
        """The full branch set for ``prepared``'s base, keyed by branch name.

        Each combo is expanded one layer at a time (:meth:`_apply_combo`), and EVERY
        intermediate layer is registered as its own branch tool. The bare base is
        appended when no combo produced it, so the base tool always binds.
        """
        # A branch preserves the base's output shape only when EVERY extension
        # in its stack does (one TRANSFORMER anywhere reshapes the result).
        extend_tools: dict[str, _Branch] = {}
        for extensions in self._tool_registry.tool_extensions_iterator(prepared.orig_name):
            self._extension_registry.validate(extensions)
            extend_tools.update(self._apply_combo(prepared, extensions))

        if prepared.orig_name not in extend_tools:
            # The base tool auto-derives its own output schema on
            # registration, so it never needs propagation (preserves=False).
            extend_tools[prepared.orig_name] = _Branch(prepared.func, prepared.orig_desc, False, False)
        return extend_tools

    def _apply_combo(self, prepared: _BranchBase, extensions: Any) -> list[tuple[str, _Branch]]:
        """Apply one combo's extension chain left-to-right.

        Returns the ``(name, branch)`` produced at EACH layer.

        Per extension: resolve name/config/factory, enforce the locality-vs-relocation
        ordering, apply the factory, reject a same-name return, enforce the schema rule,
        track whether the stack still preserves the output shape, and carry the running
        description forward.
        """
        branches: list[tuple[str, _Branch]] = []
        curr_func, curr_name, curr_desc = prepared.branch_base, prepared.orig_name, prepared.orig_desc
        stack_preserves_output = True
        stack_pauses = False
        # The relocating extension already applied in this stack, if any:
        # extensions apply left-to-right, so a later element wraps (sits
        # OUTSIDE) everything applied before it, and a relocating layer
        # ships exactly the callable it received to the worker.
        relocating_name: str | None = None
        for extension in extensions:
            # A combo element is an extension name or a ``{"name",
            # "config"}`` mapping binding author config. The registry keys
            # on the name; the config is threaded to the factory so an
            # extension closes over author-bound values (e.g.
            # ``ask_external``'s verifier) never exposed as a tool param.
            ext_name = extension_name(extension)
            ext_config = extension_config(extension)
            extension_func = self._extension_registry.get_extension(extension)
            kind = self._extension_registry.get_kind(extension)
            # A locality-requiring extension's wrapper only works in the
            # process running the tool body. Applied AFTER a relocating
            # extension it would wrap the worker-submitting stub in this
            # process — the wrapper stays behind and silently never
            # applies — so the combo is rejected loudly at bind time.
            if relocating_name is not None and self._extension_registry.requires_body_locality(extension):
                raise TaiValidationError(
                    f"tool '{prepared.orig_name}': extension '{ext_name}' requires body locality but is "
                    f"stacked outside the execution-relocating extension '{relocating_name}'; "
                    f"a locality-requiring extension must bind INSIDE the relocating one — "
                    f"place '{ext_name}' before '{relocating_name}' in the combo so its wrapper "
                    f"travels with the tool body to the worker"
                )
            if kind.relocates_execution:
                relocating_name = ext_name
            # Capture the layer's INPUT function: wrapper schema
            # enforcement compares against what this extension received,
            # not the original tool, so a transformer->wrapper stack is
            # judged against the transformer's composed schema.
            prev_func = curr_func
            # Author config is threaded (by keyword) only to a factory that
            # declares the config parameter; a config-agnostic factory keeps
            # its three-argument signature. Binding config to a factory that
            # does not accept it would silently drop the author's intent, so it
            # raises instead.
            if factory_accepts_config(extension_func):
                curr_func = extension_func(curr_func, curr_name, curr_desc, config=ext_config)
            else:
                if ext_config:
                    raise ValueError(f"extension '{ext_name}' does not accept config")
                curr_func = extension_func(curr_func, curr_name, curr_desc)

            if curr_func.__name__ == prepared.orig_name:
                raise ValueError(
                    f"Extension '{ext_name}' returned the same name '{prepared.orig_name}' as the original tool. "
                    "Extensions must return a new name to create a branch."
                )

            _enforce_extension_schema(ext_name, kind, extension_func, prev_func, curr_func, prepared.orig_name)

            stack_preserves_output = stack_preserves_output and kind.preserves_output_shape
            stack_pauses = stack_pauses or self._extension_registry.pauses(extension)
            curr_name = curr_func.__name__
            # Carry the running description forward so each stacked
            # extension composes on the previous one's output, not the
            # original. Adopt the extension's docstring only when it set a
            # NEW non-None one; a wrapper that left the docstring unchanged
            # — or has none at all (``__doc__`` is ``None``, e.g. no
            # ``functools.wraps``) — keeps the running description rather
            # than dropping it.
            if curr_func.__doc__ is not None and curr_func.__doc__ != prepared.base_doc:
                curr_desc = curr_func.__doc__
            branches.append((curr_name, _Branch(curr_func, curr_desc, stack_preserves_output, stack_pauses)))
        return branches

    def _register_expanded(
        self,
        prepared: _BranchBase,
        extend_tools: dict[str, _Branch],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        owner: str | None,
    ) -> None:
        """Bind every branch (and the bare base) onto the server with shape-aware output-schema propagation.

        Tracks each name under ``owner`` when given, and records every bound name that can pause:
        the base declares ``TOOL_META_PAUSES`` (a decorated tool through its ``meta``, a prebuilt
        tool through its own or its parent chain's), or the branch's stack holds a ``pauses=True``
        extension. A prebuilt tool's ``meta`` never rides onto its branches, so the registry, not a
        branch's own meta, is the answer.
        """
        base_pauses = (
            self._prebuilt_pauses(prepared.func)
            if isinstance(prepared.func, Tool)
            else _declares_pauses(kwargs.get("meta"))
        )
        for curr_name, branch in extend_tools.items():
            tool_func, tool_desc, preserves_output = branch.func, branch.description, branch.preserves_output
            if base_pauses or branch.stack_pauses:
                self._tool_pause_registry.register(curr_name)
            bind_kwargs = dict(kwargs)
            # Shape-aware output-schema propagation: carry the base's
            # output schema onto a branch ONLY when the branch preserves the
            # output shape (WRAPPER/BACKEND stack) AND declares none of its
            # own AND the caller did not pin one for every branch. A
            # shape-changing TRANSFORMER branch is excluded — it declares its
            # own output schema or none, never the base's.
            if (
                preserves_output
                and not isinstance(tool_func, Tool)
                and "output_schema" not in bind_kwargs
                and prepared.base_output_schema is not None
                and not _declares_own_output_schema(tool_func)
            ):
                bind_kwargs["output_schema"] = prepared.base_output_schema
            self.bind_tool(tool_func, curr_name, prepared.orig_name, *args, description=tool_desc, **bind_kwargs)
            if owner is not None:
                self._mcp_bound_tools.setdefault(owner, set()).add(curr_name)

    def _prebuilt_pauses(self, tool_obj: Tool) -> bool:
        """Whether a prebuilt tool can pause: it or a tool in its ``parent_tool`` chain declares or is recorded."""
        current: Tool | None = tool_obj
        while current is not None:
            if _declares_pauses(current.meta) or current.name in self._tool_pause_registry:
                return True
            current = current.parent_tool if isinstance(current, TransformedTool) else None
        return False

    def bind_tool(self, func, curr_name, orig_name, *args, description: str | None = None, **kwargs):
        self._tool_registry.register_extend_tool(orig_name, curr_name)

        if isinstance(func, Tool):
            # A prebuilt Tool object is registered DIRECTLY so its typed schema
            # (hidden baked args, remaining real arg types/descriptions) survives
            # unchanged; wrapping it in a function would flatten that to one opaque
            # blob. Its own name/description/tags ride on the object.
            return add_surface_tool(self._fast_mcp, func)
        if not callable(func):
            raise TypeError(
                f"cannot bind {type(func).__name__} as tool {curr_name!r}: expected a callable or a FastMCP Tool object"
            )

        # Build the tool as the secret-revealing subclass and register it — the
        # single MCP-facing seam every ``tools/call`` result flows through. FastMCP's
        # ``.tool()`` is ``from_function`` + ``add_tool``; building the subclass here
        # reproduces that while making the MCP edge reveal wrapped secrets. ``*args``
        # carries no positional here (a name is passed by keyword), so it is unused.
        tool_obj = _SecretRevealingTool.from_function(
            func,
            name=curr_name,
            description=description if description is not None else inspect.getdoc(func),
            **kwargs,
        )
        return add_surface_tool(self._fast_mcp, tool_obj)

    def _branch_base_callable(self, tool_obj: Tool) -> Callable[..., Any]:
        """The callable an extension branch wraps when the bound base is a prebuilt Tool object.

        A ``FunctionTool`` exposes its real ``fn`` directly. A ``TransformedTool``
        (a preset's baked tool) has no plain callable — its ``fn`` takes one opaque
        ``**kwargs`` and returns a ``ToolResult`` — so reconstruct a typed partial
        of the UNDERLYING function with the hidden baked args applied: it presents
        the remaining typed signature and returns the raw value, so a
        schema-preserving wrapper composes on it exactly as on a native tool.
        Anything else has no branchable body and raises loudly.
        """
        if isinstance(tool_obj, FunctionTool):
            return tool_obj.fn
        if isinstance(tool_obj, TransformedTool):
            return _cached_baked_partial(tool_obj)
        raise TypeError(
            f"cannot branch-bind {type(tool_obj).__name__} {tool_obj.name!r}: "
            "expected a FunctionTool or TransformedTool"
        )
