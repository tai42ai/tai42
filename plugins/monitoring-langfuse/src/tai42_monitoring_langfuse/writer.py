"""The Langfuse write/emit implementation of ``MonitoringWriter``.

Fail-safe (per the contract): the emit methods and the ``Span`` handle catch and
log, never raising into application code — except ``record_span`` with no
``trace_id`` (a caller bug), which raises. Propagation, scoping, and lifecycle
methods propagate loudly; ``current_trace_id`` returns None.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Any

from langfuse import propagate_attributes
from langfuse.langchain import CallbackHandler
from tai42_contract.monitoring import (
    DEFAULT_LEVEL,
    MonitoringLevel,
    Span,
    SpanKind,
    TraceContext,
)
from tai42_contract.secrets import mask_secrets

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.sdk_internals import emit_closed_span, set_trace_attributes

logger = logging.getLogger(__name__)

# Neutral SpanKind -> Langfuse observation as_type. EVENT goes through
# create_event; its entry here is a defensive fallback for start_span.
_AS_TYPE: dict[SpanKind, str] = {
    SpanKind.LLM: "generation",
    SpanKind.TOOL: "tool",
    SpanKind.CHAIN: "chain",
    SpanKind.EVENT: "span",
}


def _emit(payload: Any) -> Any:
    """Normalize a trace payload for the SDK: a ``SecretValue`` anywhere inside
    becomes the placeholder so a recorder never receives a real secret. No single
    SDK entry funnels every emit, so this is called at each ``input``/``output``
    site."""
    return mask_secrets(payload)


def _level_str(level: MonitoringLevel | None) -> str | None:
    if level is None:
        return None
    return level.value if isinstance(level, MonitoringLevel) else str(level)


def _trace_context_dict(trace_context: TraceContext | None) -> dict[str, str]:
    ctx: dict[str, str] = {}
    if trace_context is None:
        return ctx
    if trace_context.trace_id:
        ctx["trace_id"] = trace_context.trace_id
    if trace_context.parent_span_id:
        ctx["parent_span_id"] = trace_context.parent_span_id
    return ctx


class _NoOpSpan:
    """Returned when emission is suppressed or span setup fails; keeps call sites uniform.

    ``id`` is the empty string — no backend span, so no id. Being falsy, it reads
    as "no parent" when threaded as a child's ``parent_span_id``, not a dangling ref.
    """

    @property
    def id(self) -> str:
        return ""

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        pass

    def set_trace_metadata(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        pass


class LangfuseSpan:
    """Wraps a Langfuse observation as a contract ``Span`` handle (fail-safe)."""

    def __init__(self, obs: Any) -> None:
        self._obs = obs

    @property
    def id(self) -> str:
        return self._obs.id

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        try:
            kwargs: dict[str, Any] = {}
            if output is not None:
                kwargs["output"] = _emit(output)
            if model is not None:
                kwargs["model"] = model
            if usage_details is not None:
                kwargs["usage_details"] = usage_details
            if metadata is not None:
                kwargs["metadata"] = metadata
            if level is not None:
                kwargs["level"] = _level_str(level)
            if status_message is not None:
                kwargs["status_message"] = status_message
            if kwargs:
                self._obs.update(**kwargs)
        except Exception:
            logger.exception("LangfuseSpan.update failed")

    def set_trace_metadata(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        try:
            set_trace_attributes(self._obs, name=name, tags=tags)
        except Exception:
            logger.exception("LangfuseSpan.set_trace_metadata failed")


class LangfuseWriter:
    """Emits observations through the active project's Langfuse client."""

    def __init__(self, manager: LangfuseClientManager) -> None:
        self._m = manager

    # --- emit (fail-safe) ---------------------------------------------------

    @contextmanager
    def start_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        if self._m.is_disabled():
            yield _NoOpSpan()
            return

        cm = None
        span: Span = _NoOpSpan()
        try:
            client = self._m.active_client()
            kwargs: dict[str, Any] = {"name": name, "as_type": _AS_TYPE.get(kind, "span")}
            ctx = _trace_context_dict(trace_context)
            if ctx:
                kwargs["trace_context"] = ctx
            if input is not None:
                kwargs["input"] = _emit(input)
            if model is not None:
                kwargs["model"] = model
            if model_parameters is not None:
                kwargs["model_parameters"] = model_parameters
            if metadata is not None:
                kwargs["metadata"] = metadata
            # The SDK context manager opens the observation and sets it as the
            # current OTel span (so nested emits parent under it); on exit it
            # detaches and ends it.
            cm = client.start_as_current_observation(**kwargs)
            span = LangfuseSpan(cm.__enter__())
        except Exception:
            logger.exception("start_span setup failed; running body without a span")
            cm = None

        try:
            yield span
        finally:
            if cm is not None:
                try:
                    # Exit with no exception info even if the body raised: the
                    # span must still detach and end cleanly (fail-safe).
                    cm.__exit__(None, None, None)
                except Exception:
                    logger.exception("start_span cleanup failed")

    def record_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        start: datetime,
        end: datetime,
        trace_context: TraceContext,
        input: Any = None,
        output: Any = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not trace_context.trace_id:
            raise ValueError(
                "record_span requires trace_context.trace_id: the explicit-time "
                "path has no ambient context to attach to"
            )
        if self._m.is_disabled():
            return
        try:
            emit_closed_span(
                self._m.active_client(),
                name=name,
                as_type=_AS_TYPE.get(kind, "span"),
                start=start,
                end=end,
                trace_id=trace_context.trace_id,
                parent_span_id=trace_context.parent_span_id,
                input=_emit(input),
                output=_emit(output),
                level=_level_str(level),
                status_message=status_message,
                model=model,
                usage_details=usage_details,
                metadata=metadata,
            )
        except Exception:
            logger.exception("record_span failed")

    def create_event(
        self,
        *,
        name: str,
        level: MonitoringLevel = DEFAULT_LEVEL,
        trace_context: TraceContext | None = None,
        input: Any = None,
        output: Any = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._m.is_disabled():
            return
        try:
            kwargs: dict[str, Any] = {"name": name, "level": _level_str(level)}
            ctx = _trace_context_dict(trace_context)
            if ctx:
                kwargs["trace_context"] = ctx
            if input is not None:
                kwargs["input"] = _emit(input)
            if output is not None:
                kwargs["output"] = _emit(output)
            if status_message is not None:
                kwargs["status_message"] = status_message
            if metadata is not None:
                kwargs["metadata"] = metadata
            self._m.active_client().create_event(**kwargs)
        except Exception:
            logger.exception("create_event failed")

    def update_current_span(
        self,
        *,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        output: Any = None,
    ) -> None:
        if self._m.is_disabled():
            return
        try:
            kwargs: dict[str, Any] = {}
            if level is not None:
                kwargs["level"] = _level_str(level)
            if status_message is not None:
                kwargs["status_message"] = status_message
            if metadata is not None:
                kwargs["metadata"] = metadata
            if output is not None:
                kwargs["output"] = _emit(output)
            if kwargs:
                self._m.active_client().update_current_span(**kwargs)
        except Exception:
            logger.exception("update_current_span failed")

    @contextmanager
    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        if self._m.is_disabled():
            yield
            return

        cm = None
        try:
            self._m._ensure_built()
            cm = propagate_attributes(trace_name=name, tags=tags, metadata=metadata)
            cm.__enter__()
        except Exception:
            logger.exception("trace_attributes setup failed")
            cm = None

        try:
            yield
        finally:
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    logger.exception("trace_attributes teardown failed")

    # --- guard query (returns None, never raises) ---------------------------

    def current_trace_id(self) -> str | None:
        try:
            return self._m.active_client().get_current_trace_id()
        except Exception:
            logger.exception("current_trace_id failed")
            return None

    # --- propagation / callbacks (propagate loudly) --------------------------

    def inject_context(self, ctx: TraceContext) -> dict[str, Any]:
        config: dict[str, Any] = {"monitoring_provider": "langfuse"}
        configurable: dict[str, str] = {}
        if ctx.trace_id:
            configurable["monitoring_trace_id"] = ctx.trace_id
        if ctx.parent_span_id:
            configurable["monitoring_parent_span_id"] = ctx.parent_span_id
        if configurable:
            config["configurable"] = configurable
        if ctx.tags or ctx.metadata:
            merged: dict[str, Any] = {}
            if ctx.tags:
                # ``langfuse_tags`` is langfuse's own metadata key: the langchain
                # CallbackHandler reads it to set trace tags, then strips it.
                merged["langfuse_tags"] = ctx.tags
            if ctx.metadata:
                merged.update(ctx.metadata)
            config["metadata"] = merged
        return config

    def get_monitoring_callbacks(self, ctx: TraceContext) -> list[object]:
        if self._m.is_disabled():
            return []
        self._m._ensure_built()
        trace_context = _trace_context_dict(ctx)
        handler = CallbackHandler(
            public_key=self._m.resolve_public_key(),
            trace_context=trace_context or None,  # type: ignore[arg-type]  # the SDK TraceContext is a TypedDict
        )
        handler.run_inline = True
        return [handler]

    # --- scoping / suppression / lifecycle (propagate loudly) ----------------

    def scope(self, public_key: str) -> AbstractContextManager[None]:
        return self._m.scope(public_key)

    def disable(self) -> AbstractContextManager[None]:
        return self._m.disable()

    def flush(self) -> None:
        self._m.flush()

    def shutdown(self) -> None:
        self._m.shutdown()
