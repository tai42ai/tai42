"""The ``retrieval_tools_agent`` — a tools agent that retrieves tools on demand.

Instead of binding every tool up front, this agent embeds each tool's description
into a vector store and lets the model pull the relevant ones in via a
``retrieve_tools`` semantic-search tool (see
:mod:`tai42_agents.retrieval_tools_agent.graph`) — useful when the tool set is large.

``_build`` resolves the providers, embeds the tools, compiles the graph, and
returns ``(agent, messages, config, llm)`` (the ``llm`` powers a structured
finalization pass when a ``response_format`` was requested). ``astream`` projects
the run into the contract ``StreamEvent`` taxonomy; ``run`` drains it.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tai42_contract.agent import Agent, MessageDelta, MessageFinal, StreamEvent, StructuredFinal
from tai42_contract.agent.base import PresetSpec
from tai42_contract.app import tai42_app
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.embedding import get_embedding_async
from tai42_kit.llm.models import get_llm_async
from tai42_kit.llm.runtime import build_agent_input, validate_structured_output
from tai42_kit.llm.settings import embedding_settings, llm_provider_settings, llm_settings
from tai42_kit.llm.store.store_registry import store_registry

from tai42_agents._internal.append import awrite_thread_messages, require_thread_id, to_thread_messages
from tai42_agents._internal.config_util import build_run_config, init_langgraph_config
from tai42_agents._internal.recovery import _repair_dangling_tool_calls
from tai42_agents._internal.reject import (
    reject_blank_memory_keys,
    reject_unhonored,
    reject_untitled_response_format,
)
from tai42_agents._internal.render import render_message
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph
from tai42_agents.retrieval_tools_agent.prompt import RETRIEVAL_SYSTEM_MESSAGE
from tai42_agents.settings import agents_limits_settings

# Embedding dimensionality is deterministic per (provider, kwargs), so it is cached
# to avoid a probe embed call on every run. The cached value is a plain ``int``,
# safe to reuse across event loops. The cache is an LRU bounded by
# ``TAI_AGENTS_EMBEDDING_DIMS_CACHE_SIZE`` so an attacker-fillable ``embedding_kwargs``
# cannot grow it without bound.
_embedding_dims_cache: OrderedDict[tuple[str, str], int] = OrderedDict()


async def _embedding_dims(provider: str, embedding: Embeddings, kwargs: dict[str, Any] | None) -> int:
    key = (provider, json.dumps(kwargs, sort_keys=True) if kwargs else "")
    cached = _embedding_dims_cache.get(key)
    if cached is not None:
        _embedding_dims_cache.move_to_end(key)
        return cached
    dims = len(await embedding.aembed_query("check-dims"))
    _embedding_dims_cache[key] = dims
    size = agents_limits_settings().embedding_dims_cache_size
    while len(_embedding_dims_cache) > size:
        _embedding_dims_cache.popitem(last=False)
    return dims


def _terminal_result(text: str) -> str:
    """Return the ``result`` string of the run's terminal status envelope.

    The model ends every step with a status JSON object
    ``{"status","message","result"}``, and the projection concatenates the text of
    every step, so ``text`` may hold several objects back to back. The concatenation
    is parsed object by object and the LAST one's ``result`` is returned; a malformed
    or absent terminal envelope raises loudly.
    """
    decoder = json.JSONDecoder()
    idx, length = 0, len(text)
    terminal: Any = None
    found = False
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            obj, idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise ValueError(f"retrieval agent terminal message is not valid status JSON: {text!r}") from exc
        terminal = obj
        found = True
    if not found:
        raise ValueError(f"retrieval agent terminal message has no status JSON content: {text!r}")
    if not isinstance(terminal, dict):
        raise ValueError(f"retrieval agent terminal payload is not a JSON object: {text!r}")
    status = terminal.get("status")
    if status not in ("success", "error"):
        raise ValueError(f"retrieval agent terminal status must be success/error, got {status!r} in {text!r}")
    if "result" not in terminal:
        raise ValueError(f"retrieval agent terminal envelope has no 'result' field: {text!r}")
    result = terminal["result"]
    if result is None:
        return ""
    return result if isinstance(result, str) else str(result)


class RetrievalToolsAgentInput(BaseModel):
    """JSON tool-face parameters for ``retrieval_tools_agent``.

    Live ``tools`` are deliberately absent — they are an API-only ``astream``
    input, never part of the JSON tool schema. ``extra="forbid"`` rejects any
    unknown key loudly at validation rather than letting a typo at the run door
    vanish silently.

    ``response_format`` is a JSON-Schema dict with a required top-level ``"title"``
    (used as the structured-output name); when set, a structured finalization pass
    forces the terminal result into the schema and :meth:`run` returns the validated
    structured object.

    ``base_url``/``api_key`` in ``llm_kwargs``/``embedding_kwargs`` legitimately
    route to a caller-chosen model/embedding endpoint; expose any agent or tool
    carrying these kwargs only to trusted callers — an injected parent agent could
    redirect the model/embedding call to a hostile endpoint and leak the
    key/context.
    """

    model_config = ConfigDict(extra="forbid")

    user_message: str = ""
    user_message_id: str = ""
    user_message_kwargs: dict[str, Any] | None = None
    system_message: str = ""
    system_message_id: str = ""
    system_message_kwargs: dict[str, Any] | None = None
    tool_names: list[str] = Field(default_factory=list)
    presets: list[PresetSpec] = Field(default_factory=list)
    response_format: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the forced structured output (needs a top-level 'title')."
    )
    user_content_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-block keys merged into the user message's text block (e.g. cache_control "
            "for Anthropic prompt caching). Provider-unknown keys surface as loud provider errors. "
            "On a checkpointed thread the model call keeps only the newest mark (older marks are "
            "stripped), so per-turn marking stays within the provider's breakpoint cap (Anthropic: 4)."
        ),
    )
    tools_limit: int = 10
    overwrite_store: bool = False
    llm_provider: str | None = None
    embedding_provider: str | None = None
    checkpoint_provider: str | None = None
    store_provider: str | None = None
    llm_kwargs: dict[str, Any] | None = None
    embedding_kwargs: dict[str, Any] | None = None

    @field_validator("user_content_kwargs")
    @classmethod
    def _empty_content_kwargs_is_unset(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """An empty dict carries no content-block keys — normalize {} to None so it
        reads as unset, matching the builders that treat {} as no mark."""
        return value or None


@tai42_app.agents.agent("retrieval_tools_agent", tags={"agents"})
class RetrievalToolsAgent(Agent):
    """A tools agent that retrieves its tools on demand from a vector store."""

    tool_name: ClassVar[str] = "retrieval_tools_agent"
    tool_description: ClassVar[str] = (
        "A tools agent that does not load all tools up front: it embeds every tool's "
        "description into a vector store and retrieves the relevant tools on demand via a "
        "semantic search, then uses them step by step until the task is complete."
    )
    ToolInput: ClassVar[type[BaseModel]] = RetrievalToolsAgentInput

    async def _build(
        self,
        *,
        tools: Sequence[StructuredTool] = (),
        tool_names: Sequence[str] = (),
        presets: Sequence[PresetSpec] = (),
        system_message: str = "",
        system_message_id: str = "",
        system_message_kwargs: dict[str, Any] | None = None,
        user_message: str = "",
        user_message_id: str = "",
        user_message_kwargs: dict[str, Any] | None = None,
        embedding_provider: str | None = None,
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        store_provider: str | None = None,
        overwrite_store: bool = False,
        tools_limit: int = 10,
        embedding_kwargs: dict[str, Any] | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any], dict[str, Any], Any]:
        """Resolve providers, embed the tools, compile the graph, and return
        ``(agent, messages, config, llm)``. The ``llm`` is handed back so ``astream``
        can run a structured finalization pass over the terminal result when a
        ``response_format`` was requested. ``user_content_kwargs`` (e.g.
        ``cache_control``) carries content-block keys onto the user message."""
        rendered_system = await render_message(system_message, system_message_id, system_message_kwargs)
        rendered_user = await render_message(user_message, user_message_id, user_message_kwargs, allow_empty=False)

        resolved_tools = await resolve_tools(tai42_app.tools, list(tool_names), list(tools), list(presets))

        provider_settings = llm_provider_settings()

        llm_provider = llm_provider or provider_settings.llm
        llm = await get_llm_async(provider=llm_provider, **llm_settings().with_fallbacks(llm_kwargs or {}))

        embedding_provider = embedding_provider or provider_settings.embedding
        embedding = await get_embedding_async(
            provider=embedding_provider, **embedding_settings().with_fallbacks(embedding_kwargs or {})
        )

        checkpoint_provider = checkpoint_provider or provider_settings.checkpoint
        store_provider = store_provider or provider_settings.store

        dims = await _embedding_dims(embedding_provider, embedding, embedding_kwargs)
        store_kwargs = {"index": {"embed": embedding, "dims": dims, "fields": ["description"]}}
        store = await store_registry().get_store(
            provider=store_provider, conn_string=provider_settings.store_conn_string, **store_kwargs
        )

        checkpointer = await checkpoint_registry().get_checkpointer(
            provider=checkpoint_provider, conn_string=provider_settings.checkpoint_conn_string
        )

        # The system prompt is per-run graph configuration, prepended at each
        # model call inside the graph — never part of the checkpointed input.
        system_prompt = RETRIEVAL_SYSTEM_MESSAGE.format(system_message=rendered_system)
        agent = await RetrievalToolsGraph(
            tools=resolved_tools,
            llm=llm,
            store=store,
            checkpoint=checkpointer,
            overwrite_store=overwrite_store,
            tools_limit=tools_limit,
            system_prompt=system_prompt,
        ).abuild()

        config = init_langgraph_config(config)
        messages = build_agent_input(rendered_user, user_content_kwargs=user_content_kwargs)
        # The single spot both faces (run drains astream) build through, so a thread
        # poisoned by an aborted turn is repaired here before the run.
        await _repair_dangling_tool_calls(agent, config)
        return agent, messages, config, llm

    async def astream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        """Build the retrieval graph and yield its run as normalized events.

        The projection's ``MessageDelta``/``MessageFinal`` carry the model's raw
        per-step status envelopes, not a user-facing answer, so they are suppressed;
        the run instead ends with a single ``MessageFinal`` carrying only the
        terminal envelope's ``result`` string. Every other event passes through.

        With a ``response_format`` set, the terminal result is instead forced into
        the schema by a finalization pass over the resolved ``llm``, ending with one
        ``StructuredFinal`` carrying the validated object.

        ``thread_id`` / ``resume_checkpoint_id`` map into the run config's
        ``configurable`` and ``recursion_limit`` onto its top level.
        ``user_content_kwargs`` merges content-block keys (e.g. ``cache_control``)
        onto the user message's text block; a provider-unknown key surfaces as a loud
        provider error. The ABC parameters this runtime cannot honor — including
        ``system_content_kwargs`` (the system prompt is a composed template string,
        never a content block) — are rejected loudly (see ``_UNHONORED_REASONS``); the
        same guard runs on ``run``.
        """
        reject_unhonored(
            "retrieval_tools_agent.astream",
            kwargs,
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys(
            "retrieval_tools_agent.astream",
            thread_id=kwargs.get("thread_id"),
            resume_checkpoint_id=kwargs.get("resume_checkpoint_id"),
        )
        reject_untitled_response_format("retrieval_tools_agent", kwargs.get("response_format"))

        build_kwargs = {name: value for name, value in kwargs.items() if name in _BUILD_PARAMS}
        # The public ``langgraph_config`` overlays this agent's ``_build`` ``config``
        # argument, so a thread pin / recursion bound is honored over either door.
        build_kwargs["config"] = build_run_config(
            kwargs.get("langgraph_config"),
            kwargs.get("thread_id"),
            kwargs.get("resume_checkpoint_id"),
            kwargs.get("recursion_limit"),
        )

        agent, messages, config, llm = await self._build(**build_kwargs)
        terminal: MessageFinal | None = None
        async for event in aproject_agent_events(agent, messages, config):
            if isinstance(event, MessageDelta):
                continue
            if isinstance(event, MessageFinal):
                terminal = event
                continue
            yield event
        if terminal is None:
            raise ValueError(
                "retrieval agent run produced no terminal message; the required status envelope was never emitted"
            )
        result_text = _terminal_result(terminal.text)
        response_format = kwargs.get("response_format")
        if response_format is not None:
            structured = await llm.with_structured_output(response_format, include_raw=False).ainvoke(
                [HumanMessage(content=result_text)]
            )
            yield StructuredFinal(data=validate_structured_output(structured, response_format))
            return
        yield MessageFinal(text=result_text)

    async def run(self, **kwargs: Any) -> Any:
        """Reject the ABC parameters this runtime cannot honor, then drain
        ``astream`` to the final value.

        A ``response_format`` is honored: its JSON-Schema dict must carry a top-level
        ``"title"``, and the drain returns the validated structured object, raising
        loudly if the run produced none.
        """
        reject_unhonored(
            "retrieval_tools_agent.run",
            kwargs,
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys(
            "retrieval_tools_agent.run",
            thread_id=kwargs.get("thread_id"),
            resume_checkpoint_id=kwargs.get("resume_checkpoint_id"),
        )
        response_format = kwargs.get("response_format")
        reject_untitled_response_format("retrieval_tools_agent", response_format)
        return await self._drain(self.astream(**kwargs), response_format=response_format)

    async def _compile_for_append(
        self,
        llm_provider: str | None,
        checkpoint_provider: str | None,
        store_provider: str | None,
        llm_kwargs: dict[str, Any] | None,
    ) -> Any:
        """Compile the retrieval graph bound to the resolved checkpointer for an append.

        Resolves the model and checkpointer the same way ``_build`` does — so the
        append reaches the SAME saver a run resolves — and compiles the graph with no
        tools. Tool embedding and the vector index serve only tool retrieval, never
        the message checkpoint, so neither the embedding provider nor the index is
        resolved here and no model call is made; the store is resolved plain only
        because the graph requires one to compile.
        """
        provider_settings = llm_provider_settings()
        llm_provider = llm_provider or provider_settings.llm
        llm = await get_llm_async(provider=llm_provider, **llm_settings().with_fallbacks(llm_kwargs or {}))
        store_provider = store_provider or provider_settings.store
        store = await store_registry().get_store(
            provider=store_provider, conn_string=provider_settings.store_conn_string
        )
        checkpoint_provider = checkpoint_provider or provider_settings.checkpoint
        checkpointer = await checkpoint_registry().get_checkpointer(
            provider=checkpoint_provider, conn_string=provider_settings.checkpoint_conn_string
        )
        return await RetrievalToolsGraph(tools=[], llm=llm, store=store, checkpoint=checkpointer).abuild()

    async def append_thread_messages(
        self,
        *,
        thread_id: str,
        messages: list[dict[str, str]],
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        store_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        """Append ``messages`` to the thread's stored history without running the model.

        ``messages`` items are ``{"role": "user"|"assistant", "content": str}`` — an
        unknown role or blank content raises. ``thread_id`` (or a
        ``configurable.thread_id`` carried in ``langgraph_config``) names the thread to
        append to; a missing one raises rather than minting a fresh thread. The graph
        compiles over the resolved checkpointer (see :meth:`_compile_for_append`) so
        the append lands on the SAME checkpointed thread a run reads.
        """
        converted = to_thread_messages(messages)
        reject_blank_memory_keys(
            "retrieval_tools_agent.append_thread_messages", thread_id=thread_id, resume_checkpoint_id=None
        )
        config = build_run_config(langgraph_config, thread_id)
        require_thread_id("retrieval_tools_agent.append_thread_messages", config)
        agent = await self._compile_for_append(llm_provider, checkpoint_provider, store_provider, llm_kwargs)
        await awrite_thread_messages(agent, config, converted)


# The ABC ``run``/``astream`` parameters this runtime cannot honor, mapped to the
# reason named in the raised error. Honored params are absent: ``thread_id`` /
# ``resume_checkpoint_id`` / ``recursion_limit`` map into the run config, the tool
# inputs / provider / message params go through ``_BUILD_PARAMS``, and
# ``response_format`` is consumed by the finalization pass in ``astream``.
_UNHONORED_REASONS: dict[str, str] = {
    "subagents": "sub-agent delegation is langchain_deep_agent's domain; this agent never exposes sub-agents",
    "strategy": "it applies no composition strategy and will not silently ignore one",
    "skills": "it loads no skills backend",
    "inline_skills": "it loads no skills backend",
    "interrupt_on": "its graph never pauses for external input, so there is no interrupt to configure",
    "resume": "its graph never interrupts, so there is no paused run to resume",
    "system_content_kwargs": (
        "its system prompt is a composed template string prepended inside the graph, never built as a "
        "content block through build_system_message, so it cannot carry content-block keys; "
        "use user_content_kwargs instead"
    ),
}

# The unhonored parameters whose unset default is an empty collection; every other
# defaults to ``None`` and is set when not ``None``.
_UNHONORED_COLLECTION_PARAMS = frozenset({"subagents", "skills", "inline_skills"})


# The ``_build`` parameters ``astream`` forwards from a ``run``/``astream`` call.
# The honored memory params and ``recursion_limit`` are absent (mapped into the run
# config before building); the unhonored params raise via ``_UNHONORED_REASONS``.
_BUILD_PARAMS = frozenset(
    {
        "tools",
        "tool_names",
        "presets",
        "system_message",
        "system_message_id",
        "system_message_kwargs",
        "user_message",
        "user_message_id",
        "user_message_kwargs",
        "embedding_provider",
        "llm_provider",
        "checkpoint_provider",
        "store_provider",
        "overwrite_store",
        "tools_limit",
        "embedding_kwargs",
        "llm_kwargs",
        "user_content_kwargs",
    }
)
