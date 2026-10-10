"""``vqa_agent`` as an :class:`Agent` — visual question answering.

The simplest agent: a single multimodal LLM completion. It takes an image
reference (public URL or storage id) and a text query, builds one ``HumanMessage``
carrying both, and streams the model's answer — no tools, checkpointer, or graph.
The image is resolved through ``tai42_app.storage.resolve_image`` (a stored id
resolves to a base64 data-URI, a public URL passes through) and the agent builds
its model's image content part from it before the model call.

* :meth:`astream` — projects the model's own ``astream`` into the contract
  vocabulary: :class:`MessageDelta` per token, a :class:`RunUsage` when the provider
  reports token counts, then one assembled :class:`MessageFinal`. Usage precedes the
  final so a consumer stopping at the first ``final=True`` still sees the counts.
* :meth:`run` — drains :meth:`astream` and returns the final answer text.

With a ``response_format`` set, the completion emits exactly one
:class:`StructuredFinal` instead. No reasoning, tool-call, or interrupt events arise
— the agent runs no loop, tools, or graph.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field
from tai42_contract.agent import AddressedMemory, Agent
from tai42_contract.agent.events import MessageDelta, MessageFinal, StreamEvent, StructuredFinal
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.llm import bind_run_trace
from tai42_kit.llm.models import get_llm_async
from tai42_kit.llm.runtime import validate_structured_output
from tai42_kit.llm.settings import llm_provider_settings, llm_settings
from tai42_kit.llm.structured import plan_structured_output

from tai42_agents._internal.outcomes import RepromptCapError, outcome_for_drive_error
from tai42_agents._internal.reject import resolve_response_format
from tai42_agents._internal.run_kwargs import guard_run_kwargs
from tai42_agents._internal.structured import ainvoke_structured
from tai42_agents._internal.usage import usage_event


async def _vqa_message(image_url: str, query: str) -> HumanMessage:
    """One multimodal ``HumanMessage`` carrying the text query and the image.

    ``image_url`` is resolved through the storage facet (a public URL passes through, a
    storage id resolves to a base64 data-URI) and carried as LangChain's ``image_url`` part.
    """
    ref = await tai42_app.storage.resolve_image(image_url)
    image_part = {"type": "image_url", "image_url": {"url": ref.url}}
    return HumanMessage(content=[{"type": "text", "text": query}, image_part])


class VqaAgentInput(BaseModel):
    """JSON tool-face parameters for ``vqa_agent``.

    ``image_url`` accepts either a public image URL or a storage id — both are a
    plain ``str``, resolved by the storage facet's ``resolve_image``.

    ``response_format`` is a JSON-Schema dict with a required top-level ``"title"``
    (used as the structured-output name); when set, the completion forces the model
    to emit output matching it and :meth:`run` returns the validated structured
    object.

    ``base_url``/``api_key`` in ``llm_kwargs`` legitimately route to a caller-chosen
    model endpoint; expose any agent or tool carrying these kwargs only to trusted
    callers — an injected parent agent could redirect the model call to a hostile
    endpoint and leak the key/context.

    ``extra="forbid"`` rejects any unknown key loudly at validation rather than
    letting a typo at the run door vanish silently.
    """

    model_config = ConfigDict(extra="forbid")

    image_url: str
    query: str
    response_format: TemplatedText | dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the forced structured output (needs a top-level 'title')."
    )
    llm_provider: str | None = None
    llm_kwargs: dict[str, Any] | None = None


# The contract ``Agent.run`` keys ``vqa_agent`` cannot honor, mapped to the reason named in the raised
# error. A single multimodal completion has no tool loop, graph, or checkpointer.
_UNHONORED_REASONS: dict[str, str] = {
    "tool_names": "it runs a single multimodal completion and loads no tools",
    "user_message": "it builds its own prompt from image_url and query and takes no user_message",
    "thread_id": "it runs no checkpointer, so there is no thread to pin",
}


def _guard(
    face: str,
    unknown: Mapping[str, Any],
    *,
    thread_id: str | None,
    user_message: TemplatedText | None,
    tool_names: Sequence[str],
    response_format: Any,
) -> None:
    guard_run_kwargs(
        face,
        unknown,
        {
            "thread_id": thread_id,
            "user_message": user_message,
            "tool_names": tool_names,
            "response_format": response_format,
        },
        _UNHONORED_REASONS,
    )


@tai42_app.agents.agent("vqa_agent", tags={"agents"})
class VqaAgent(Agent):
    """An agent that answers a query about an image using a multimodal LLM."""

    tool_name: ClassVar[str] = "vqa_agent"
    tool_description: ClassVar[str] = "Analyze an image and answer a query about it using a multimodal LLM."
    ToolInput: ClassVar[type[BaseModel]] = VqaAgentInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        """A single completion addresses no memory."""
        return AddressedMemory()

    async def run(
        self,
        *,
        image_url: str,
        query: str,
        response_format: Any = None,
        llm_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        thread_id: str | None = None,
        user_message: TemplatedText | None = None,
        tool_names: Sequence[str] = (),
        **unknown: Any,
    ) -> Any:
        """Drain :meth:`astream` and return the final value.

        The agent honors ``image_url``/``query``, ``response_format``, and
        ``llm_provider``/``llm_kwargs``. A ``response_format`` JSON-Schema dict must
        carry a top-level ``"title"``; when set, the validated structured object is
        returned and a run producing none raises loudly. A set ``thread_id`` /
        ``user_message`` / ``tool_names`` or an undeclared keyword is refused loudly, in
        parity with :meth:`astream`.
        """
        _guard(
            "vqa_agent.run",
            unknown,
            thread_id=thread_id,
            user_message=user_message,
            tool_names=tool_names,
            response_format=response_format,
        )
        response_format = await resolve_response_format("vqa_agent", response_format)
        return await self._drain(
            self.astream(
                image_url=image_url,
                query=query,
                response_format=response_format,
                llm_provider=llm_provider,
                llm_kwargs=llm_kwargs,
            ),
            response_format=response_format,
        )

    async def astream(
        self,
        *,
        image_url: str,
        query: str,
        response_format: Any = None,
        llm_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        thread_id: str | None = None,
        user_message: TemplatedText | None = None,
        tool_names: Sequence[str] = (),
        **unknown: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one multimodal completion.

        Without a ``response_format`` the answer streams as token
        :class:`MessageDelta`s, then the run's usage (when the provider reports it),
        then the assembled final :class:`MessageFinal`. Usage precedes the final so a
        consumer stopping at the first ``final=True`` still sees the counts. Chunks
        are accumulated so usage totals sum across a split stream.

        With a ``response_format`` the completion forces structured output via
        ``with_structured_output`` and emits exactly one :class:`StructuredFinal`.

        A set ``thread_id`` / ``user_message`` / ``tool_names`` or an undeclared keyword is
        refused loudly here, in parity with :meth:`run`.
        """
        _guard(
            "vqa_agent.astream",
            unknown,
            thread_id=thread_id,
            user_message=user_message,
            tool_names=tool_names,
            response_format=response_format,
        )
        response_format = await resolve_response_format("vqa_agent", response_format)
        provider = llm_provider or llm_provider_settings().llm
        llm = await get_llm_async(provider=provider, **llm_settings().with_fallbacks(llm_kwargs or {}))

        message = await _vqa_message(image_url, query)

        # This agent calls the model directly (no graph), so it binds the run's trace here
        # and threads the per-invoke config onto each model call — the single-shot
        # structured door and the streaming text door alike — so both record under the
        # run's trace through the same seam the graph agents reach.
        trace = bind_run_trace()

        if response_format is not None:
            # The capability-negotiated plan routes native-first (grammar-enforced) or to
            # the bounded tool tier; ``ainvoke_structured`` runs the capped re-prompt loop,
            # ending a never-conforming run on the typed outcome instead of a raw raise.
            plan = plan_structured_output(llm, provider, response_format)
            try:
                structured = await ainvoke_structured(llm, plan, [message], config=trace.config)
            except RepromptCapError as exc:
                outcome = outcome_for_drive_error(exc, None)
                if outcome is None:
                    raise
                yield outcome
                return
            yield StructuredFinal(data=validate_structured_output(structured, response_format))
            return

        parts: list[str] = []
        accumulated: Any = None
        async for chunk in llm.astream([message], config=cast(RunnableConfig, trace.config)):
            accumulated = chunk if accumulated is None else accumulated + chunk
            text = chunk.text
            if text:
                parts.append(text)
                yield MessageDelta(text=text)

        if accumulated is not None:
            usage = usage_event(accumulated)
            if usage is not None:
                yield usage
        final = "".join(parts).strip()
        if final:
            yield MessageFinal(text=final)
