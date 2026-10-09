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

Live in-process tools are the contract's framework-neutral ``ClientTool`` handles
(``tai42_contract.tools``); a framework adapter lives with the agent that uses it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar, Final

from pydantic import BaseModel, Field

from tai42_contract.agent.events import (
    InterruptFinal,
    MessageFinal,
    RecursionLimitFinal,
    StreamEvent,
    StructuredFinal,
    StructuredOutputUnresolvedFinal,
    SuspendedFinal,
    final_event_for_value,
)
from tai42_contract.errors import ErrorKind
from tai42_contract.template import TemplatedText

#: The longest tool name the platform binds to a model: the strictest bound among the kit's providers
#: (OpenAI function names).
TOOL_NAME_MAX_LEN: Final[int] = 64

#: The preset-name law: a preset name becomes a live tool name and a ``{name}`` path segment.
PRESET_NAME_PATTERN: Final[str] = rf"^[A-Za-z0-9_-]{{1,{TOOL_NAME_MAX_LEN}}}$"


class AgentInterruptedError(Exception):
    """Raised by ``run``/``_drain`` when a non-streaming caller drains a run that paused on an interrupt.

    A non-streaming caller cannot act on an interrupt, so it surfaces loudly rather than
    returning a partial. ``interrupts`` is the list of :class:`InterruptFinal` events the run
    emitted.
    """

    # A drained run that paused on an interrupt did not complete — cancelled.
    __tai_error_kind__ = ErrorKind.CANCELLED

    def __init__(self, interrupts: list[InterruptFinal]):
        """Carry the :class:`InterruptFinal` events the drained run paused on into the error."""
        self.interrupts = interrupts
        ids = ", ".join(i.interrupt_id for i in interrupts)
        super().__init__(f"agent run interrupted (pending: {ids or 'unknown'})")


def _resolve_drain_terminal(
    *,
    suspended: SuspendedFinal | None,
    interrupts: list[InterruptFinal],
    outcome: StructuredOutputUnresolvedFinal | RecursionLimitFinal | None,
    structured: StructuredFinal | None,
    message: MessageFinal | None,
    response_format: Any,
) -> Any:
    """The terminal rule of a drained event stream — the final value from the collected terminals.

    A park wins first — returned as a :class:`~tai42_contract.interactions.SuspendedInteraction`
    typed value (a clean, non-error outcome; a RETURN is a success or a still-parked typed value,
    never a failure) — then an interrupt raises, then a typed non-fatal ``outcome``
    (structured-output re-prompt cap reached, or the recursion limit hit) is returned as itself — it
    is the run's answer, so it precedes the requested-but-absent ``response_format`` raise. Else the
    requested-but-absent ``response_format`` raises, else the last structured/message payload, else
    ``""`` for an empty run. Never returns a partial.
    """
    if suspended is not None:
        return suspended.to_suspended_interaction()
    if interrupts:
        raise AgentInterruptedError(interrupts)
    if outcome is not None:
        return outcome
    if response_format is not None:
        if structured is None:
            raise RuntimeError("agent run requested a response_format but produced no structured output")
        return structured.data
    if structured is not None:
        return structured.data
    if message is not None:
        return message.text
    return ""


class PresetSpec(BaseModel):
    """A base tool bound to fixed kwargs, bound as one more tool of the run that carries it.

    ``name`` follows :data:`PRESET_NAME_PATTERN`. A base tool that interprets its fixed kwargs as a
    nested document carries that document opaquely here.
    """

    name: str = Field(pattern=PRESET_NAME_PATTERN)
    description: str = ""
    base_tool: str
    fixed_kwargs: dict[str, Any] = Field(default_factory=dict)


class AddressedMemory(BaseModel, frozen=True):
    """The memory a set of run kwargs addresses, as the agent reads them.

    ``keys`` names the run kwargs present that steer THIS run's own thread or checkpoint
    continuity (resuming, the checkpoint the run continues from, the checkpointer it runs on) — the
    keys a door that owns the run's identity refuses. A key that only configures a sub-role's memory
    is not one of them; its thread and checkpoint ids are still reported in ``thread_ids`` /
    ``checkpoint_ids``.
    """

    keys: frozenset[str] = frozenset()
    thread_ids: frozenset[str] = frozenset()
    checkpoint_ids: frozenset[str] = frozenset()


class Agent(ABC):
    """The uniform agent contract.

    Implement :meth:`run`; override :meth:`astream` only when the agent produces real per-step
    events.

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

    :attr:`tool_names` is the FIXED tool set this agent always binds — the
    statically-declared set a bind check reads to judge what the agent will run
    with (e.g. whether it can ask its caller). ``None`` (the default) declares an
    agent that resolves its tool set per call (from its ``ToolInput``/run kwargs),
    so nothing static is known. An agent whose tool set is fixed declares it here;
    like ``spec_runnable`` it is the implementation's own declaration, never
    inferred.

    :attr:`parks` declares that this agent can suspend on an async ask raised by a
    tool it runs and resume out of band (it emits ``SuspendedFinal``). Like
    ``spec_runnable`` it is the implementation's own declaration, never inferred; a
    consumer reads it to know whether a run of this agent can park.
    """

    tool_name: ClassVar[str]
    tool_description: ClassVar[str] = ""
    ToolInput: ClassVar[type[BaseModel]]
    spec_runnable: ClassVar[bool] = False
    preset_bakeable_fields: ClassVar[frozenset[str]] = frozenset()
    tool_names: ClassVar[Sequence[str] | None] = None
    # The door ``extras`` keys this agent target reads when a door starts it. The visit checks a
    # door's ``extras`` against this set before the run and refuses an undeclared key. The empty
    # default declares an agent that reads no extras.
    extras_keys: ClassVar[frozenset[str]] = frozenset()
    parks: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        """Report the memory ``run_kwargs`` (the output of :meth:`from_tool_input`) address.

        The names of the run kwargs present that steer the run's memory or continuity (``keys``),
        and every thread id and checkpoint id they carry, however nested. The platform reserves
        thread namespaces and decides caller pinning from this answer, so an agent that carries a
        thread or checkpoint anywhere in its run kwargs MUST report it. An agent with no memory
        returns ``AddressedMemory()``. The contract's own ``thread_id`` keyword is checked by the
        platform itself and need not be reported.
        """
        ...

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
        thread_id: str | None = None,
        user_message: TemplatedText | None = None,
        # Sequence + empty-tuple default: immutable, so no mutable default argument.
        tool_names: Sequence[str] = (),
        response_format: TemplatedText | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run the agent once and return its final value.

        ``thread_id``, ``user_message``, ``tool_names`` and ``response_format`` are the keys the
        platform supplies or reads; every other keyword is the agent's own, declared by its
        ``ToolInput`` and documented by the agent. ``response_format``: a JSON-Schema ``dict`` MUST
        carry a non-empty top-level ``title`` (the structured-output name the run forces), and each
        ``oneOf`` variant its own; a ``TemplatedText`` names a stored schema rendered at the point of
        use; an in-process pydantic class (an agent's own widening of this parameter) carries its
        class name.
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
        yield final_event_for_value(result)

    async def append_thread_messages(self, *, thread_id: str, messages: list[dict[str, str]], **kwargs: Any) -> None:
        """Append ``messages`` to ``thread_id``'s stored history WITHOUT running the agent.

        Each item is ``{"role": "user"|"assistant", "content": str}``. The conversation
        bridge calls this to record a manual-mode inbound and an operator's outbound reply
        into the thread's memory, so a later agent turn reads them as prior context. An
        agent with no thread memory raises :class:`NotImplementedError`.
        """
        raise NotImplementedError

    async def _drain(self, agen: AsyncIterator[StreamEvent], *, response_format: Any = None) -> Any:
        """Consume a whole event stream and return the final value (the terminal rule).

        Streaming agents' ``run`` uses this over their own ``astream``.

        Semantics:

        * A :class:`SuspendedFinal` seen → return a
          :class:`~tai42_contract.interactions.SuspendedInteraction` typed value (the run
          parked on an async ask and resumes out of band; it did NOT fail, so this never
          raises). A park takes precedence over an interrupt — the two never coexist in one
          pause, but the park-before-raise order keeps a park a clean, non-error outcome.
        * Any :class:`InterruptFinal` seen → raise :class:`AgentInterruptedError`
          (a non-streaming caller cannot answer an interrupt).
        * A :class:`StructuredOutputUnresolvedFinal` or :class:`RecursionLimitFinal`
          seen → return that typed, non-fatal outcome as itself (the run's answer);
          it precedes the requested-but-absent ``response_format`` raise.
        * ``response_format`` requested but no :class:`StructuredFinal` produced
          → raise (fail loud; never silently fall back to message text).
        * Else return the last terminal's payload — ``StructuredFinal.data`` if
          any, else the last ``MessageFinal.text``.
        * No terminal at all → return ``""`` (an empty run). Never return a
          partial.
        """
        interrupts: list[InterruptFinal] = []
        suspended: SuspendedFinal | None = None
        outcome: StructuredOutputUnresolvedFinal | RecursionLimitFinal | None = None
        structured: StructuredFinal | None = None
        message: MessageFinal | None = None
        async for event in agen:
            if isinstance(event, SuspendedFinal):
                suspended = event
            elif isinstance(event, InterruptFinal):
                interrupts.append(event)
            elif isinstance(event, StructuredOutputUnresolvedFinal | RecursionLimitFinal):
                outcome = event
            elif isinstance(event, StructuredFinal):
                structured = event
            elif isinstance(event, MessageFinal):
                message = event
        return _resolve_drain_terminal(
            suspended=suspended,
            interrupts=interrupts,
            outcome=outcome,
            structured=structured,
            message=message,
            response_format=response_format,
        )
