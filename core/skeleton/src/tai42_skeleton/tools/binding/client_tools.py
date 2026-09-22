"""Builds langchain client tools over bound tools for in-process agent dispatch."""

import inspect
import logging
import warnings
from collections.abc import Callable
from typing import Any, cast

from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool
from langchain_core.tools import StructuredTool, ToolException, tool
from tai42_contract.interactions import (
    NestedParkOwnershipError,
    SuspendedInteraction,
    resolve_park_adoption,
    suspended_interaction_marker,
)
from tai42_contract.secrets import mask_secrets

from tai42_skeleton.agent.binding import _UNSET
from tai42_skeleton.tools.binding.arguments import _named_call_arguments
from tai42_skeleton.tools.binding.branch import _BranchBindingMixin
from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.resolution import _ResolutionMixin
from tai42_skeleton.tools.binding.schema import _VAR_PARAM_KINDS
from tai42_skeleton.tools.context_bridge import bridge_context
from tai42_skeleton.tools.tier import enforce_run_tier

logger = logging.getLogger(__name__)

# Model providers cap a LangChain client tool's function name at 64 characters;
# a client-facing tool name is truncated to this length, and a post-truncation
# collision is a hard error.
CLIENT_TOOL_NAME_MAX_LEN = 64


class _ClientToolsMixin(_ResolutionMixin, _BranchBindingMixin):
    """Exposes bound tools as langchain ``StructuredTool``s an in-process agent invokes.

    Gates each dispatch on the execution identity and masks secrets.
    """

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]:
        tools = await self.get_tools()
        for name in names or []:
            if name not in tools:
                raise UnknownToolError(name)

        selected = {name: t for name, t in tools.items() if not names or name in names}
        truncated: dict[str, str] = {}
        for name in selected:
            key = name[:CLIENT_TOOL_NAME_MAX_LEN]
            if key in truncated:
                raise ValueError(
                    f"Tool names {truncated[key]!r} and {name!r} collide after "
                    f"{CLIENT_TOOL_NAME_MAX_LEN}-char truncation."
                )
            truncated[key] = name

        client_tools: list[StructuredTool] = []
        for name, t in selected.items():
            # An opaque-signature tool (an agent run tool's ``**arguments`` body)
            # cannot have its input schema inferred from the signature — that
            # advertises NO fields — so pass its explicit ``.parameters`` schema and
            # its own description (langchain does not fall back to the impl's
            # docstring once a dict ``args_schema`` is given, and the ``**arguments``
            # body carries none). A normal tool gets neither: langchain infers the
            # schema from the presented signature and reads the description from the
            # runnable's docstring.
            explicit_schema = self._client_args_schema(t)
            extra_kwargs: dict[str, Any] = (
                {"args_schema": explicit_schema, "description": t.description} if explicit_schema is not None else {}
            )
            # langchain's overloaded ``tool`` is typed to return ``BaseTool | Callable``
            # and to want a ``Runnable`` for ``runnable``; this call shape (name + a
            # plain callable) always builds a ``StructuredTool`` at runtime.
            with warnings.catch_warnings():
                # Inferring a client tool's schema rebuilds a pydantic model from the
                # presented signature, so an operation carrying a field named ``schema``
                # (a wire key mapped by FIELD NAME, whose name shadows pydantic's
                # deprecated ``BaseModel.schema()`` alias) re-emits the shadowing warning
                # here — outside the model definition site that suppresses it. Suppressed
                # narrowly at this rebuild site, matching the message the operation and
                # contract models suppress at their own definition.
                warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)
                built = cast(
                    StructuredTool,
                    tool(
                        name_or_callable=name[:CLIENT_TOOL_NAME_MAX_LEN],
                        runnable=cast(Any, self._client_runnable(t)),
                        **extra_kwargs,
                    ),
                )
            client_tools.append(built)
        return client_tools

    def _client_args_schema(self, tool_obj: Tool) -> dict[str, Any] | None:
        """The explicit input JSON schema a client tool must advertise, or ``None`` to use langchain inference.

        Needed when the tool's fn signature cannot be round-tripped through langchain's inferred args model;
        otherwise ``None`` (langchain infers the schema from the presented signature).
        Two signature shapes need the explicit ``.parameters`` instead of
        inference:

        * a bare ``*args``/``**kwargs`` passthrough — inference advertises NO
          fields, hiding every input from the LLM;
        * a synthesized agent run tool, whose concrete per-field signature carries
          the non-JSON-serializable :data:`_UNSET` sentinel as the default of every
          optional parameter (so the body can forward set fields only). Building a
          langchain args model over that signature would materialize and then fail
          to serialize the sentinel, so the tool advertises its explicit
          ``.parameters`` (the agent's exact ``ToolInput`` schema) and is invoked
          through a permissive runnable (:meth:`_client_runnable`).

        A tool with an ordinary concrete signature returns ``None`` and keeps the
        signature-inference path (injected Context/``Depends`` stripping included)
        unchanged.
        """
        if not isinstance(tool_obj, FunctionTool):
            return None
        resolved = without_injected_parameters(tool_obj.fn)
        params = list(inspect.signature(resolved).parameters.values())
        all_var = bool(params) and all(p.kind in _VAR_PARAM_KINDS for p in params)
        has_unset_default = any(p.default is _UNSET for p in params)
        if all_var or has_unset_default:
            return tool_obj.parameters
        return None

    def _client_runnable(self, tool_obj: Tool) -> Callable[..., Any]:
        """The callable langchain builds a client tool over, for an in-process agent to invoke.

        Resolves the tool's typed callable via ``_branch_base_callable``
        (``FunctionTool`` → ``fn``; a preset ``TransformedTool`` → its baked
        partial; anything else raises), then strips the fastmcp-injected
        Context/``Depends`` params with ``without_injected_parameters`` so the
        schema langchain infers advertises only the real user args — never a
        ``Context`` the LLM would be asked to supply. The returned closure carries
        that stripped ``__signature__``/``__name__`` (langchain infers the args
        schema via ``inspect.signature``, which honors ``__signature__``) and runs
        the call under ``bridge_context`` so an injected ``ctx.elicit()`` /
        ``ctx.sample()`` resolves through the platform bridges, exactly as
        ``run_tool`` does.

        When the tool advertises an explicit ``args_schema`` (:meth:`_client_args_schema`
        returns a schema — an all-VAR passthrough or a synthesized agent run tool
        with :data:`_UNSET` sentinel defaults), the runnable is left PERMISSIVE (its
        native ``*args``/``**kwargs`` signature): the LLM-facing schema rides on the
        explicit ``args_schema``, and forwarding only the caller-supplied kwargs
        keeps ``from_tool_input``'s set-fields-only contract intact — never
        materializing (and failing to serialize) the sentinel defaults.

        The closure gates on the execution identity exactly as ``run_tool`` does, under
        the tool's FULL registered name (the client-facing truncation is only a label).
        With no identity bound the call forwards straight through to the snapshot's
        callable.

        Under a fire the body is re-resolved from the name LIVE, so the registration
        decided about is the one that runs — a client-tool snapshot outlives an agent
        turn, and a preset re-based or deleted mid-turn would otherwise run its stale
        baked body; a vanished registration fails loudly as an unknown tool.
        """
        base_callable = self._branch_base_callable(tool_obj)
        resolved = without_injected_parameters(base_callable)
        resolved_sig = inspect.signature(resolved)

        async def runnable(*args, **kwargs):
            # Run-time tier fence: this agent tool-dispatch door reaches the tool BODY
            # directly (never the ``run_tool`` seam), so it enforces the same admin fence for
            # a ``fenced``/``secret`` tool here — through the SAME ``enforce_run_tier`` the
            # seam uses, resolving a preset/branch to its base tool identically, so the two
            # doors cannot drift. A non-fenced tool resolves no caller (a dict read).
            await enforce_run_tier(self._app, tool_obj.name)
            target, target_sig = resolved, resolved_sig
            execution_identity = self._bound_execution_identity()
            if execution_identity is not None:
                target = without_injected_parameters(
                    self._branch_base_callable(await self._resolve_run_target(tool_obj.name))
                )
                target_sig = inspect.signature(target)
                await self._authorize_execution_dispatch(
                    execution_identity, tool_obj.name, _named_call_arguments(target_sig, args, kwargs)
                )
            with bridge_context(self._app.fastmcp):
                # Only the tool BODY's own failure becomes a model-visible tool error; the
                # machinery above (identity gate, live re-resolution, bridge setup) AND the
                # park-adoption/masking below stay a raw abort. So the broad catch wraps the body
                # invocation ALONE — a bug in the adoption check, marker build, or secret masking
                # is not mislabeled as a tool that failed. CancelledError/BaseException pass
                # through untouched.
                try:
                    result = target(*args, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                except Exception as exc:
                    logger.warning("in-process tool %r failed: %s", tool_obj.name, exc, exc_info=exc)
                    raise ToolException(f"Error calling tool {tool_obj.name!r}: {exc}") from exc
                if isinstance(result, SuspendedInteraction):
                    # An async ask parked the caller and returned this sentinel. Inside a
                    # graph the tool task must COMPLETE (so ask runs exactly once, never
                    # replayed on resume), so convert the sentinel to the reserved contract
                    # marker: the ToolMessage commits carrying it, and the in-graph park
                    # middleware recognizes the park by this RESULT shape (never a tool name) and
                    # interrupts once for the whole super-step.
                    #
                    # WHICH park this run records is the ownership question: its own
                    # interaction when the ask was raised under this run's binding, or —
                    # when the caller CHAINED this dispatch — the chained key naming the
                    # CALL, owned by this run's own resume continuation, so the nested run
                    # keeps its park while its terminal re-enters this run through the
                    # chain's delivery tool. A nested run's park is never ADOPTED:
                    # unchained, the sentinel is refused here as a model-visible tool
                    # error rather than suspending this run behind a resume fired only at
                    # the nested run.
                    #
                    # The park deadline rides through unchanged, so a chained park
                    # INHERITS the horizon of the ask it is waiting on.
                    try:
                        park_key, park_owner = resolve_park_adoption(
                            result.resume_owner, interaction_id=result.interaction_id, tool_name=tool_obj.name
                        )
                    except NestedParkOwnershipError as exc:
                        # A DELIBERATE refusal, not a tool that failed: it is already worded for
                        # the model and already names the tool, so it is re-raised as its own
                        # message — no second "Error calling tool" prefix, and no traceback-bearing
                        # WARNING for a decision this seam made on purpose.
                        logger.info("in-process tool %r returned a park this run does not own: %s", tool_obj.name, exc)
                        raise ToolException(str(exc)) from exc
                    # The owner rides the WIRE form too: the driver that later claims this park
                    # reads it off the serialized ToolMessage, never off the sentinel. The per-ask
                    # id lists ride it as well so a driver surfacing a whole step can merge them.
                    return suspended_interaction_marker(
                        park_key,
                        result.expiry_at,
                        park_owner,
                        interaction_ids=result.interaction_ids,
                        caller_interaction_ids=result.caller_interaction_ids,
                    )
                # The model never sees a secret value: this adapter feeds the langchain layer (the
                # model, the checkpoint, the callback trace), so a wrapped secret is masked before
                # it leaves here.
                return mask_secrets(result)

        runnable.__name__ = resolved.__name__
        # langchain reads the runnable's docstring for the client tool's
        # description (it raises without one), so carry the resolved callable's.
        runnable.__doc__ = resolved.__doc__

        if self._client_args_schema(tool_obj) is not None:
            # Explicit args_schema advertised: keep the permissive signature so
            # langchain forwards only the supplied kwargs to the concrete body.
            return runnable

        runnable.__signature__ = resolved_sig  # type: ignore[attr-defined]
        # langchain infers the args schema via pydantic, which reads BOTH the
        # signature AND ``get_type_hints`` (i.e. ``__annotations__``). Carry the
        # presented signature's annotations so every advertised param has a type;
        # a bare ``**kwargs`` closure otherwise raises a ``KeyError`` at inference.
        annotations = {
            pname: p.annotation
            for pname, p in resolved_sig.parameters.items()
            if p.annotation is not inspect.Parameter.empty
        }
        if resolved_sig.return_annotation is not inspect.Signature.empty:
            annotations["return"] = resolved_sig.return_annotation
        runnable.__annotations__ = annotations
        return runnable
