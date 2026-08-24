"""The generic ``Agent`` contract.

Every agent in the ecosystem implements this one interface. ``run`` is the
primitive (a single value); ``astream`` is a free default over ``run`` that
streaming agents override with real per-step events.

Two faces are derived from one ``Agent`` class:

* a JSON ``run`` tool (LLM / MCP / plugin facing) — auto-generated downstream
  from :attr:`Agent.ToolInput` + :attr:`Agent.tool_description`;
* an in-process ``astream`` method (API / SSE facing) that can take live tools.

``astream`` is never a JSON tool: an LLM can't consume a stream, and the deep
path needs live in-process tool closures. So a developer writes ONLY the
``Agent`` class — no hand-written tool wrapper.

Pure-contract note: the live-tool field ``SubAgentSpec.tools`` is typed
``list[Any]`` here. Its concrete impl holds live ``langchain_core`` ``StructuredTool``
objects, but a vendor type may not sit in a runtime pydantic field of a
pydantic-only contract (a contract carries the shape, not the live objects).
``Agent.run``'s ``tools=``/``StructuredTool`` parameter annotations stay vendor-named
— they are lazy strings under ``from __future__ import annotations`` and only the
type-checker (which gets the vendor lib as a dev dep) ever reads them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.agent.events import (
    InterruptFinal,
    MessageFinal,
    StreamEvent,
    StructuredFinal,
    SuspendedFinal,
)
from tai42_contract.errors import ErrorKind

if TYPE_CHECKING:
    from langchain_core.tools import StructuredTool


class AgentInterruptedError(Exception):
    """Raised by ``run``/``_drain`` when a non-streaming caller drains a run that
    paused on an interrupt. A non-streaming caller cannot act on an interrupt, so
    it surfaces loudly rather than returning a partial. ``interrupts`` is the list
    of :class:`InterruptFinal` events the run emitted."""

    # A drained run that paused on an interrupt did not complete — cancelled.
    __tai_error_kind__ = ErrorKind.CANCELLED

    def __init__(self, interrupts: list[InterruptFinal]):
        self.interrupts = interrupts
        ids = ", ".join(i.interrupt_id for i in interrupts)
        super().__init__(f"agent run interrupted (pending: {ids or 'unknown'})")


class PresetSpec(BaseModel):
    """A base tool bound to fixed kwargs, resolved into a ``StructuredTool`` at
    run time (see ``resolve_tools``). A base tool that interprets its fixed kwargs
    as a nested document carries that document opaquely here.
    """

    name: str
    description: str = ""
    base_tool: str
    fixed_kwargs: dict[str, Any] = Field(default_factory=dict)


class SubAgentSpec(BaseModel):
    """Neutral sub-agent descriptor — the in-process, live-tools shape.

    Carries name/description/system_prompt/tool_names/tools(live)/presets/skills/
    inline_skills/response_format/strategy/subagents — the fields a live sub-agent
    (e.g. the mcp-finder) needs. ``inline_skills`` items are plain dicts
    (``{"name", "content"}``). JSON callers use the richer ``DeepSubAgentSpec``
    (full field set) instead of this type.

    ``tools`` is ``list[Any]`` in the contract (live ``StructuredTool`` objects in
    the impl) — a vendor type cannot be a runtime pydantic field here.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str = ""
    system_prompt: str = ""
    tool_names: list[str] = []
    tools: list[Any] = []
    presets: list[PresetSpec] = []
    skills: list[str] = []
    inline_skills: list[dict[str, Any]] = []
    response_format: type[BaseModel] | dict[str, Any] | None = None
    strategy: str | None = None
    subagents: list[SubAgentSpec] = []


class Agent(ABC):
    """The uniform agent contract. Implement :meth:`run`; override
    :meth:`astream` only when the agent produces real per-step events.

    Subclasses declare three class attributes that drive auto-tool generation:

    * :attr:`tool_name` — the registered name (also the auto-generated tool name);
    * :attr:`tool_description` — the LLM-facing description (carried, not lost);
    * :attr:`ToolInput` — a JSON-able pydantic model of the tool params. Live
      ``tools=`` is deliberately NOT in ``ToolInput`` (API-only via ``astream``);
      a non-JSON field fails loudly at ``model_json_schema()`` time.

    :attr:`spec_runnable` is a capability marker (default ``False``):
    ``spec_runnable = True`` declares that this agent's ``ToolInput`` advertises
    EXACTLY the composable fields its runtime honors — no more, no less. This lets
    a generic UI gate authoring controls on the input schema's fields: it renders a
    control only for an advertised (honored) field, and baking an un-advertised
    field is rejected at author time. The declaration is the implementation's — it
    is never inferred, and no agent name is ever hardcoded. Because every
    ``ToolInput`` field of a ``spec_runnable`` agent is honored, all of them are
    preset-bakeable; :attr:`preset_bakeable_fields` covers the other case.

    :attr:`preset_bakeable_fields` names the ``ToolInput`` fields whose baked
    (preset ``fixed_kwargs``) value the runtime honors on an agent that is NOT
    UI-composable — declaring a field here implies no UI control at all, only that
    a hidden baked value is passed through and acted on. A field is preset-bakeable
    iff the agent is ``spec_runnable`` (all fields, above) OR the field is listed
    here. Like ``spec_runnable`` it is the implementation's own declaration, never
    inferred; every listed name must be a real ``ToolInput`` field. The empty
    default means nothing is bakeable unless the agent is ``spec_runnable``.
    """

    tool_name: ClassVar[str]
    tool_description: ClassVar[str] = ""
    ToolInput: ClassVar[type[BaseModel]]
    spec_runnable: ClassVar[bool] = False
    preset_bakeable_fields: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def from_tool_input(cls, validated: BaseModel) -> dict[str, Any]:
        """Map a validated :attr:`ToolInput` instance to ``run`` kwargs.

        Passes through every set field with nested pydantic values (``presets``,
        ``subagents``, ``inline_skills``) kept as model instances, since ``run``
        and the resolvers it calls read them by attribute. ``model_dump`` is
        deliberately NOT used — it would flatten those nested models to dicts.
        Agents whose JSON tool-face shape differs from their ``run`` signature
        override this.

        Any error an override raises must be a constant, hand-authored message and
        must NOT interpolate input values: the agent run routes surface these
        messages verbatim to the caller, and the baked input can carry credentials
        the caller is not entitled to see.
        """
        return {name: getattr(validated, name) for name in validated.model_fields_set}

    @abstractmethod
    async def run(
        self,
        *,
        # Sequence + empty-tuple defaults: immutable, so no mutable default
        # argument and no cast from () to a list type.
        tools: Sequence[StructuredTool] = (),
        tool_names: Sequence[str] = (),
        presets: Sequence[PresetSpec] = (),
        subagents: Sequence[SubAgentSpec] = (),
        system_message: str = "",
        user_message: str = "",
        response_format: Any = None,
        strategy: str | None = None,
        interrupt_on: dict[str, Any] | None = None,
        skills: Sequence[str] = (),
        inline_skills: Sequence[dict[str, Any]] = (),
        recursion_limit: int | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        resume_checkpoint_id: str | None = None,
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        store_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run the agent once and return its final value.

        Tool inputs (``tools``/``tool_names``/``presets``) are uniform only for
        tools-agent-shaped agents; role-specific agents (voting/vqa/refine) read
        their own params off ``**kwargs``. ``response_format`` is
        typed ``Any`` because the deep path passes a JSON-Schema ``dict`` while
        the tools-agent path passes a pydantic class. ``resume_checkpoint_id``
        forks past an aborted turn, distinct from ``resume`` (the interrupt
        answer).
        """
        ...

    async def astream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        """Free default for non-streaming agents: run once, emit one terminal.

        The terminal type matches the result: a ``str`` becomes a
        :class:`MessageFinal` (plain-text answer), any other value a
        :class:`StructuredFinal` (the structured ``response_format`` object).
        Streaming agents override this with the real per-step projection and
        implement :meth:`run` by draining their own ``astream`` via
        :meth:`_drain`.
        """
        result = await self.run(**kwargs)
        if isinstance(result, str):
            yield MessageFinal(text=result)
        else:
            yield StructuredFinal(data=result)

    async def append_thread_messages(self, *, thread_id: str, messages: list[dict[str, str]], **kwargs: Any) -> None:
        """Append ``messages`` to ``thread_id``'s stored history WITHOUT running the agent.

        Each item is ``{"role": "user"|"assistant", "content": str}``. The conversation
        bridge calls this to record a manual-mode inbound and an operator's outbound reply
        into the thread's memory, so a later agent turn reads them as prior context. An
        agent with no thread memory raises :class:`NotImplementedError`.
        """
        raise NotImplementedError

    async def _drain(self, agen: AsyncIterator[StreamEvent], *, response_format: Any = None) -> Any:
        """Consume a whole event stream and return the final value (the terminal
        rule). Streaming agents' ``run`` uses this over their own ``astream``.

        Semantics:

        * A :class:`SuspendedFinal` seen → return the suspended RECEIPT dict
          ``{"status": "suspended", ...}`` (the run parked on an async ask and
          resumes out of band; it did NOT fail, so this never raises). A park takes
          precedence over an interrupt — the two never coexist in one pause, but the
          receipt-before-raise order keeps a park a clean, non-error outcome.
        * Any :class:`InterruptFinal` seen → raise :class:`AgentInterruptedError`
          (a non-streaming caller cannot answer an interrupt).
        * ``response_format`` requested but no :class:`StructuredFinal` produced
          → raise (fail loud; never silently fall back to message text).
        * Else return the last terminal's payload — ``StructuredFinal.data`` if
          any, else the last ``MessageFinal.text``.
        * No terminal at all → return ``""`` (an empty run). Never return a
          partial.
        """
        interrupts: list[InterruptFinal] = []
        suspended: SuspendedFinal | None = None
        structured: StructuredFinal | None = None
        message: MessageFinal | None = None
        async for event in agen:
            if isinstance(event, SuspendedFinal):
                suspended = event
            elif isinstance(event, InterruptFinal):
                interrupts.append(event)
            elif isinstance(event, StructuredFinal):
                structured = event
            elif isinstance(event, MessageFinal):
                message = event
        if suspended is not None:
            return {
                "status": "suspended",
                "interaction_ids": suspended.interaction_ids,
                "thread_id": suspended.thread_id,
                "expiry_at": suspended.expiry_at,
            }
        if interrupts:
            raise AgentInterruptedError(interrupts)
        if response_format is not None:
            if structured is None:
                raise RuntimeError("agent run requested a response_format but produced no structured output")
            return structured.data
        if structured is not None:
            return structured.data
        if message is not None:
            return message.text
        return ""
