"""LangChain / LangGraph callbacks → monitoring writer records.

:class:`MonitoringCallbackHandler` turns every chain, model, tool and retriever run of one
bound run into a record through the contract writer (``open_span`` … ``end``), each span the
current span of its run's context so producer code inside a node reaches it.

Model and tool runs are recorded in full: a model call's input and output messages in the
OpenTelemetry GenAI message shape, plus the generated message's full record under
``GENERATION_MESSAGE_METADATA_KEY``; a tool call's arguments and result.

Chain runs never copy the run data. In ``"references"`` mode each chain value is recorded
as a manifest: a value already recorded is a ``payload_ref`` to the record where it first
appeared (by object identity), everything else is walked (at most ``_MAX_DEPTH`` levels) or
recorded inline once and registered as the origin of later references. An object whose
top-level members were rebound since its origin record (the shallow snapshot taken at
registration differs) is written member by member — unchanged members as references into
the origin, changed ones walked — and re-registered at the new record. A rebound nested
deeper than the walk inside a container left whole is not detected: a reference to that
container resolves to its form at its origin record. In ``"producer"`` mode chain runs carry
no input/output (the producer sets them from inside the run with ``update_current_span``).
A run declared through :func:`declared_chain_payload` records the producer's ``build``
result in either mode.

Every callback is fail-safe: a failure is logged at ERROR and never raised into the run.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.errors import GraphBubbleUp
from langgraph.types import Send
from pydantic import BaseModel
from tai42_contract.monitoring import (
    GENERATION_MESSAGE_METADATA_KEY,
    STEP_ROLE_METADATA_KEY,
    MonitoringLevel,
    MonitoringWriter,
    Span,
    SpanKind,
    StepRole,
    TokenUsage,
    TraceContext,
)

from tai42_kit.monitoring import escape_pointer_token, payload_ref

__all__ = ["ChainPayloads", "MonitoringCallbackHandler", "declared_chain_payload"]

logger = logging.getLogger(__name__)

ChainPayloads = Literal["references", "producer"]
Field = Literal["input", "output"]

# How deep a chain value is walked before a member is kept whole.
_MAX_DEPTH = 4
# A string longer than this is registered (and later referenced) rather than repeated inline.
_INLINE_STR_MAX = 256
_HIDDEN_TAG = "langsmith:hidden"
_INVOCATION_PARAMS_NOT_PARAMETERS = frozenset({"model", "model_name", "tools", "_type"})

_DECLARED: ContextVar[Mapping[UUID, Callable[[Any], Any]]] = ContextVar(
    "tai42_declared_chain_payload", default=MappingProxyType({})
)


@contextmanager
def declared_chain_payload(run_id: UUID, build: Callable[[Any], Any]) -> Iterator[None]:
    """Record ``build(inputs)`` / ``build(outputs)`` as the payloads of LangChain run ``run_id`` started in the block.

    The producer passes ``config["run_id"] = run_id`` to the run it starts (LangChain names
    exactly that one run with it). ``build`` is called while the run's span is current, so
    it may read ``writer.current_span_id()``.
    """
    token = _DECLARED.set(MappingProxyType({**_DECLARED.get(), run_id: build}))
    try:
        yield
    finally:
        _DECLARED.reset(token)


def _is_scalar_inline(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and len(value) <= _INLINE_STR_MAX


def _shallow_members(obj: Any) -> tuple[Any, ...]:
    """The object's top-level members in the form the encoder writes it; ``()`` for an immutable leaf."""
    if isinstance(obj, BaseModel):
        fields = type(obj).model_fields
        members = [(name, getattr(obj, name)) for name, info in fields.items() if not info.exclude]
        members.extend((obj.model_extra or {}).items())
        return tuple(members)
    if isinstance(obj, dict):
        return tuple(obj.items())  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(obj, (list, tuple)):
        return tuple(obj)  # pyright: ignore[reportUnknownArgumentType]
    return ()


def _same(current: Any, snapshot: Any) -> bool:
    if current is snapshot:
        return True
    try:
        return bool(current == snapshot)
    except Exception:
        return False


def _members_unchanged(current: tuple[Any, ...], snapshot: tuple[Any, ...]) -> bool:
    return len(current) == len(snapshot) and all(_same(c, s) for c, s in zip(current, snapshot, strict=True))


def _keyed(obj: Any, members: tuple[Any, ...]) -> list[tuple[str, Any]]:
    if isinstance(obj, (BaseModel, dict)):
        return [(str(k), v) for k, v in members]
    return [(str(i), v) for i, v in enumerate(members)]


_NOT_WALKED: Any = object()


class _Origin:
    __slots__ = ("field", "members", "obj", "pointer", "ref", "span_id")

    def __init__(self, obj: Any, span_id: str, field: Field | Literal["metadata"], pointer: str) -> None:
        self.obj = obj  # held so its id is never reused while mapped
        self.span_id = span_id
        self.field: Literal["input", "output", "metadata"] = field
        self.pointer = pointer
        self.ref = payload_ref(span_id, field, pointer)
        self.members = _shallow_members(obj)


class _Run:
    __slots__ = ("build", "kind", "name", "span")

    def __init__(self, span: Span, name: str, kind: SpanKind, build: Callable[[Any], Any] | None) -> None:
        self.span = span
        self.name = name
        self.kind = kind
        self.build = build


def _run_name(serialized: Mapping[str, Any] | None, kwargs: Mapping[str, Any], default: str) -> str:
    name = kwargs.get("name")
    if name:
        return str(name)
    if serialized:
        if serialized.get("name"):
            return str(serialized["name"])
        ids = serialized.get("id")
        if isinstance(ids, list) and ids:
            return str(ids[-1])  # pyright: ignore[reportUnknownArgumentType]
    return default


_ROLES = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "content": content}] if content else []
    parts: list[dict[str, Any]] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append({"type": "text", "content": block})
        elif isinstance(block, dict) and block.get("type") == "text" and "text" in block:  # pyright: ignore[reportUnknownMemberType]
            parts.append({"type": "text", "content": block["text"]})
        else:
            parts.append(block)
    return parts


def _message_parts(message: BaseMessage) -> list[dict[str, Any]]:
    if isinstance(message, ToolMessage):
        return [{"type": "tool_call_response", "id": message.tool_call_id, "response": message.content}]
    parts = _content_parts(message.content)
    if isinstance(message, AIMessage):
        parts.extend(
            {"type": "tool_call", "id": call.get("id"), "name": call["name"], "arguments": call["args"]}
            for call in message.tool_calls
        )
    return parts


def gen_ai_message(message: BaseMessage) -> dict[str, Any]:
    """``message`` in the OpenTelemetry GenAI message shape (``role`` + ``parts``)."""
    role = _ROLES.get(message.type) or str(getattr(message, "role", message.type))
    return {"role": role, "parts": _message_parts(message)}


def _usage(response: LLMResult, message: BaseMessage | None) -> TokenUsage | None:
    usage_metadata = getattr(message, "usage_metadata", None) if message is not None else None
    if usage_metadata:
        return TokenUsage(
            input_tokens=usage_metadata.get("input_tokens"),
            output_tokens=usage_metadata.get("output_tokens"),
            total_tokens=usage_metadata.get("total_tokens"),
        )
    token_usage = (response.llm_output or {}).get("token_usage")
    if isinstance(token_usage, Mapping) and token_usage:
        return TokenUsage(
            input_tokens=token_usage.get("input_tokens", token_usage.get("prompt_tokens")),
            output_tokens=token_usage.get("output_tokens", token_usage.get("completion_tokens")),
            total_tokens=token_usage.get("total_tokens"),
        )
    return None


def _response_model(response: LLMResult, message: BaseMessage | None) -> str | None:
    metadata: Mapping[str, Any] = getattr(message, "response_metadata", None) or {}
    model = metadata.get("model_name") or metadata.get("model") or (response.llm_output or {}).get("model_name")
    return str(model) if model else None


class MonitoringCallbackHandler(BaseCallbackHandler):
    """Records one bound run's LangChain runs through ``writer`` under ``trace_context``.

    ``grouping_nodes`` names the framework nodes (``langgraph_node``) that hold steps rather
    than being one; their chain records carry ``StepRole.GROUPING``. ``chain_payloads``
    selects how chain runs record their input/output (see the module docstring).
    """

    run_inline = True

    def __init__(
        self,
        writer: MonitoringWriter,
        trace_context: TraceContext,
        *,
        grouping_nodes: frozenset[str] = frozenset(),
        chain_payloads: ChainPayloads = "references",
    ) -> None:
        """Bind the writer, the run's lineage and the recording options."""
        super().__init__()
        self.writer = writer
        self.trace_context = trace_context
        self.grouping_nodes = grouping_nodes
        self.chain_payloads: ChainPayloads = chain_payloads
        self._runs: dict[UUID, _Run] = {}
        self._origins: dict[int, _Origin] = {}
        self._lock = threading.Lock()

    # --- runs -------------------------------------------------------------------

    def _context_for(self, parent_run_id: UUID | None) -> TraceContext:
        parent = self._runs.get(parent_run_id) if parent_run_id is not None else None
        if parent is None or not parent.span.id:
            return self.trace_context
        return TraceContext(trace_id=self.trace_context.trace_id, parent_span_id=parent.span.id)

    def _open(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        *,
        name: str,
        kind: SpanKind,
        input_: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        build: Callable[[Any], Any] | None = None,
    ) -> _Run:
        span = self.writer.open_span(
            name=name,
            kind=kind,
            trace_context=self._context_for(parent_run_id),
            input_=input_,
            model=model,
            model_parameters=model_parameters,
            metadata=metadata,
            activate=True,
        )
        run = _Run(span, name, kind, build)
        with self._lock:
            self._runs[run_id] = run
        return run

    def _close(self, run_id: UUID, parent_run_id: UUID | None) -> None:
        with self._lock:
            run = self._runs.pop(run_id, None)
            if parent_run_id is None:
                self._origins.clear()
        if run is not None:
            run.span.end()

    def _guard(self, callback: str, run_id: UUID, body: Callable[[], object]) -> None:
        try:
            body()
        except Exception:
            run = self._runs.get(run_id)
            logger.exception(
                "monitoring callback %s failed for run %r", callback, run.name if run is not None else str(run_id)
            )

    # --- references ---------------------------------------------------------------

    def _register(self, obj: Any, span_id: str, field: Field | Literal["metadata"], pointer: str) -> None:
        self._origins[id(obj)] = _Origin(obj, span_id, field, pointer)

    def _register_unless_scalar(self, obj: Any, span_id: str, field: Field) -> None:
        if not _is_scalar_inline(obj):
            with self._lock:
                self._register(obj, span_id, field, "")

    def _manifest(self, value: Any, span_id: str, field: Field, pointer: str, depth: int) -> Any:
        origin = self._origins.get(id(value))
        if origin is not None and origin.obj is value:
            members = _shallow_members(value)
            if _members_unchanged(members, origin.members):
                return origin.ref
            form = self._member_wise(value, members, origin, span_id, field, pointer, depth)
            self._register(value, span_id, field, pointer)
            return form
        if depth < _MAX_DEPTH:
            walked = self._walk(value, span_id, field, pointer, depth)
            if walked is not _NOT_WALKED:
                return walked
        if isinstance(value, (BaseModel, dict, list, tuple)) or (
            isinstance(value, str) and len(value) > _INLINE_STR_MAX
        ):
            self._register(value, span_id, field, pointer)
        return value

    def _walk(self, value: Any, span_id: str, field: Field, pointer: str, depth: int) -> Any:
        def child(member: Any, key: str) -> Any:
            return self._manifest(member, span_id, field, f"{pointer}/{escape_pointer_token(key)}", depth + 1)

        if isinstance(value, Send):
            return {
                "node": value.node,
                "arg": child(value.arg, "arg"),
                "timeout": None if value.timeout is None else str(value.timeout),
            }
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: child(getattr(value, f.name), f.name) for f in dataclasses.fields(value)}
        if isinstance(value, dict):
            return {k: child(v, str(k)) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, (list, tuple)):
            return [child(v, str(i)) for i, v in enumerate(value)]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return _NOT_WALKED

    def _member_wise(
        self,
        value: Any,
        members: tuple[Any, ...],
        origin: _Origin,
        span_id: str,
        field: Field,
        pointer: str,
        depth: int,
    ) -> Any:
        previous = dict(_keyed(origin.obj, origin.members))
        out: list[tuple[str, Any]] = []
        for key, member in _keyed(value, members):
            token = escape_pointer_token(key)
            if key in previous and _same(member, previous[key]):
                if _is_scalar_inline(member):
                    out.append((key, member))
                else:
                    out.append((key, payload_ref(origin.span_id, origin.field, f"{origin.pointer}/{token}")))
            else:
                out.append((key, self._manifest(member, span_id, field, f"{pointer}/{token}", depth + 1)))
        if isinstance(value, (BaseModel, dict)):
            return dict(out)
        return [member for _, member in out]

    def _chain_payload(self, run: _Run, value: Any, field: Field) -> tuple[bool, Any]:
        """Whether the chain run records ``value`` for ``field``, and the payload it records."""
        if run.build is not None:
            try:
                return True, run.build(value)
            except Exception as exc:
                logger.exception("chain payload build failed for run %s", run.name)
                return True, exc
        if self.chain_payloads == "producer":
            return False, None
        with self._lock:
            return True, self._manifest(value, run.span.id, field, "", 0)

    # --- chain runs ---------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Open the chain run's span and record its input payload."""

        def body() -> None:
            name = _run_name(serialized, kwargs, "chain")
            record_metadata: dict[str, Any] = {**(metadata or {}), "tags": tags or []}
            if (metadata or {}).get("langgraph_node") == name and name in self.grouping_nodes:
                record_metadata[STEP_ROLE_METADATA_KEY] = StepRole.GROUPING
            build = _DECLARED.get().get(run_id)
            run = self._open(
                run_id, parent_run_id, name=name, kind=SpanKind.CHAIN, metadata=record_metadata, build=build
            )
            if _HIDDEN_TAG in (tags or []):
                run.span.update(level=MonitoringLevel.DEBUG)
            # The input is set on the run's own span, current in this context; a span that was
            # not opened (disabled, failed) records nothing rather than overwrite an enclosing one.
            if run.span.id and self.writer.current_span_id() == run.span.id:
                records, payload = self._chain_payload(run, inputs, "input")
                if records:
                    self.writer.update_current_span(input_=payload)

        self._guard("on_chain_start", run_id, body)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any) -> None:
        """Record the chain run's output payload and end its span."""

        def body() -> None:
            run = self._runs.get(run_id)
            if run is not None:
                records, payload = self._chain_payload(run, outputs, "output")
                if records:
                    run.span.update(output=payload)
            self._close(run_id, parent_run_id)

        self._guard("on_chain_end", run_id, body)

    def on_chain_error(
        self, error: BaseException, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """End the chain run's span: an interrupt as interrupted, any other error at ERROR."""

        def body() -> None:
            run = self._runs.get(run_id)
            if run is not None:
                if isinstance(error, GraphBubbleUp):
                    run.span.update(metadata={"interrupted": True})
                else:
                    run.span.update(level=MonitoringLevel.ERROR, status_message=str(error))
            self._close(run_id, parent_run_id)

        self._guard("on_chain_error", run_id, body)

    # --- model runs ---------------------------------------------------------------

    def _open_model(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        serialized: dict[str, Any] | None,
        messages: list[dict[str, Any]],
        kwargs: Mapping[str, Any],
    ) -> None:
        params: Mapping[str, Any] = kwargs.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name")
        parameters = {k: v for k, v in params.items() if k not in _INVOCATION_PARAMS_NOT_PARAMETERS}
        metadata: dict[str, Any] = {**(kwargs.get("metadata") or {})}
        if params.get("tools") is not None:
            metadata["tools"] = params["tools"]
        self._open(
            run_id,
            parent_run_id,
            name=_run_name(serialized, kwargs, "model"),
            kind=SpanKind.LLM,
            input_=messages,
            model=str(model) if model else None,
            model_parameters=parameters or None,
            metadata=metadata or None,
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Open the model run's span with its input messages in full."""
        self._guard(
            "on_chat_model_start",
            run_id,
            lambda: self._open_model(
                run_id, parent_run_id, serialized, [gen_ai_message(m) for batch in messages for m in batch], kwargs
            ),
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Open the completion run's span with each prompt as one user message."""
        messages = [{"role": "user", "parts": [{"type": "text", "content": p}]} for p in prompts]
        self._guard(
            "on_llm_start", run_id, lambda: self._open_model(run_id, parent_run_id, serialized, messages, kwargs)
        )

    def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """Record the generated messages in full, the usage and the generated message's full record."""

        def body() -> None:
            run = self._runs.get(run_id)
            if run is None:
                return
            generations = response.generations[0] if response.generations else []
            message = generations[0].message if generations and isinstance(generations[0], ChatGeneration) else None
            output: list[dict[str, Any]] = []
            for generation in generations:
                info = generation.generation_info or {}
                if isinstance(generation, ChatGeneration):
                    entry = gen_ai_message(generation.message)
                    finish = info.get("finish_reason") or generation.message.response_metadata.get("finish_reason")
                else:
                    entry = {"role": "assistant", "parts": [{"type": "text", "content": generation.text}]}
                    finish = info.get("finish_reason")
                entry["finish_reason"] = finish
                output.append(entry)
            run.span.update(
                output=output,
                model=_response_model(response, message),
                usage=_usage(response, message),
                metadata={GENERATION_MESSAGE_METADATA_KEY: message} if message is not None else None,
            )
            if message is not None and self.chain_payloads == "references":
                with self._lock:
                    self._register(message, run.span.id, "metadata", f"/{GENERATION_MESSAGE_METADATA_KEY}")
                    if isinstance(message, AIMessage):
                        for j, call in enumerate(message.tool_calls):
                            self._register(
                                call, run.span.id, "metadata", f"/{GENERATION_MESSAGE_METADATA_KEY}/tool_calls/{j}"
                            )
            self._close(run_id, parent_run_id)

        self._guard("on_llm_end", run_id, body)

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """End the model run's span at ERROR."""
        self._guard("on_llm_error", run_id, lambda: self._fail(run_id, parent_run_id, error))

    def _fail(self, run_id: UUID, parent_run_id: UUID | None, error: BaseException) -> None:
        run = self._runs.get(run_id)
        if run is not None:
            run.span.update(level=MonitoringLevel.ERROR, status_message=str(error))
        self._close(run_id, parent_run_id)

    # --- tool and retriever runs ----------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Open the tool run's span with its arguments in full."""

        def body() -> None:
            value = inputs if inputs is not None else input_str
            run = self._open(
                run_id, parent_run_id, name=_run_name(serialized, kwargs, "tool"), kind=SpanKind.TOOL, input_=value
            )
            if inputs is not None and self.chain_payloads == "references":
                self._register_unless_scalar(inputs, run.span.id, "input")

        self._guard("on_tool_start", run_id, body)

    def on_tool_end(self, output: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any) -> None:
        """Record the tool's result in full and end its span."""

        def body() -> None:
            run = self._runs.get(run_id)
            if run is not None:
                run.span.update(output=output)
                if self.chain_payloads == "references":
                    self._register_unless_scalar(output, run.span.id, "output")
            self._close(run_id, parent_run_id)

        self._guard("on_tool_end", run_id, body)

    def on_tool_error(
        self, error: BaseException, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """End the tool run's span at ERROR."""
        self._guard("on_tool_error", run_id, lambda: self._fail(run_id, parent_run_id, error))

    def on_retriever_start(
        self,
        serialized: dict[str, Any] | None,
        query: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Open the retriever run's span (a tool record) with its query."""
        self._guard(
            "on_retriever_start",
            run_id,
            lambda: self._open(
                run_id, parent_run_id, name=_run_name(serialized, kwargs, "retriever"), kind=SpanKind.TOOL, input_=query
            ),
        )

    def on_retriever_end(
        self, documents: Sequence[Any], *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """Record the retrieved documents and end the span."""

        def body() -> None:
            run = self._runs.get(run_id)
            if run is not None:
                run.span.update(output=list(documents))
            self._close(run_id, parent_run_id)

        self._guard("on_retriever_end", run_id, body)

    def on_retriever_error(
        self, error: BaseException, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """End the retriever run's span at ERROR."""
        self._guard("on_retriever_error", run_id, lambda: self._fail(run_id, parent_run_id, error))
