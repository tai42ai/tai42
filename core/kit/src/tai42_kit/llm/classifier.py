"""Classifier model factory and its provider-agnostic contract.

A classifier judges JSON-compatible state against typed questions and returns
typed answers — JSON in, JSON out. Three question kinds cover the common
judgments: a binary ``noul`` (the probability that the answer is yes), a
categorical ``choice`` (one label from a fixed set, with a probability for every
label), and an ordinal ``score`` (an expected value over an ordered rubric). A
request pairs the state with a named mapping of questions; the response carries
one answer per question, token usage, and the provider's request id.

These pydantic models ARE the platform's classifier contract.
:func:`get_classifier` and :func:`get_classifier_async` build a cached,
provider-keyed runnable that accepts a :class:`ClassifyRequest` and returns a
:class:`ClassifyResponse`; each provider adapts this contract to its own wire
shape behind the factory. The models take no vendor import, so the contract can
be read and constructed without any provider package installed.
"""

import asyncio
from functools import lru_cache
from typing import Annotated, Any, Literal, cast

from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from pydantic import BaseModel, Field, JsonValue, TypeAdapter
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import MonitoringLevel, SpanKind, TokenUsage

from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs
from tai42_kit.llm.run_trace import resolve_trace_context

QuestionContent = str | dict[str, JsonValue] | list[JsonValue]
"""Instruction text or structured content the classifier judges against: free text, a JSON object, or a JSON array."""


class NoulCriteria(BaseModel):
    """Optional descriptions of what the yes and no outcomes of a noul question mean."""

    true: JsonValue = None
    false: JsonValue = None


class NoulQuestion(BaseModel):
    """Binary question: judge the state and return the probability that the answer is yes."""

    type: Literal["noul"] = "noul"
    instructions: QuestionContent
    criteria: NoulCriteria | None = None


class ChoiceQuestion(BaseModel):
    """Categorical question: pick one label from ``criteria``, keyed by label to its description."""

    type: Literal["choice"] = "choice"
    instructions: QuestionContent
    criteria: dict[str, JsonValue] = Field(min_length=1)


class ScoreQuestion(BaseModel):
    """Ordinal question: rate the state against ``criteria``, an ordered rubric of two or more levels."""

    type: Literal["score"] = "score"
    instructions: QuestionContent
    criteria: list[JsonValue] = Field(min_length=2)


ClassifyQuestion = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]
"""A question of one of the three kinds, discriminated on ``type``."""


class ClassifyRequest(BaseModel):
    """The state to judge together with the named questions to answer about it."""

    state: JsonValue
    questions: dict[str, ClassifyQuestion] = Field(min_length=1)


class NoulAnswer(BaseModel):
    """The probability, from 0 to 1, that a noul question's answer is yes."""

    type: Literal["noul"]
    noul: float = Field(ge=0.0, le=1.0)


class ChoiceAnswer(BaseModel):
    """The selected label of a choice question, its per-label probabilities, and a confidence."""

    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)


class ScoreAnswer(BaseModel):
    """The expected value of a score question, the level legend and distribution, and a confidence."""

    type: Literal["score"]
    score: float
    legend: dict[int, JsonValue]
    probabilities: dict[int, float]
    confidence: float = Field(ge=0.0, le=1.0)


ClassifyAnswer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]
"""An answer of one of the three kinds, discriminated on ``type``."""


class ClassifyUsage(BaseModel):
    """Token usage reported for a classification request; either count may be unreported."""

    input_tokens: int | None = None
    output_tokens: int | None = None


class ClassifyResponse(BaseModel):
    """One answer per requested question, plus the model, token usage, and provider request id."""

    model: str
    answers: dict[str, ClassifyAnswer]
    usage: ClassifyUsage = Field(default_factory=ClassifyUsage)
    request_id: str | None = None


async def get_classifier_async(provider: str, **kwargs) -> Runnable[ClassifyRequest, ClassifyResponse]:
    """Build (or return the cached) classifier runnable for ``provider``, off the event loop."""
    return await asyncio.to_thread(get_classifier, provider=provider, **kwargs)


def get_classifier(provider: str, **kwargs) -> Runnable[ClassifyRequest, ClassifyResponse]:
    """Build (or return the cached) classifier runnable for ``provider`` with the given kwargs."""
    return _cached_classifier(provider, KwargsCacheKey(kwargs))


@lru_cache(maxsize=64)
def _cached_classifier(provider: str, kwargs_key: KwargsCacheKey) -> Runnable[ClassifyRequest, ClassifyResponse]:
    # The caller's original kwargs flow to the constructor untouched (no JSON
    # round trip); secrets are unwrapped only at this seam.
    return _build_classifier(provider, **unwrap_secret_kwargs(kwargs_key.kwargs))


def _usage(usage: ClassifyUsage) -> TokenUsage | None:
    """The response's token counts, or ``None`` when none were reported."""
    if usage.input_tokens is None and usage.output_tokens is None:
        return None
    return TokenUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


def _recording_classifier(
    pipeline: Runnable[ClassifyRequest, ClassifyResponse],
) -> Runnable[ClassifyRequest, ClassifyResponse]:
    """Wrap ``pipeline`` so every classifier call is recorded to monitoring as a MODEL call.

    A classifier is its own model kind, not a chat model, so its call never fires the
    chat-model callbacks the monitoring backend records a generation from. This seam opens
    one generation span around the whole provider pipeline (request adapter, vendor call,
    response adapter) and records, from the kit's own :class:`ClassifyResponse`, the model
    id, the token usage, and the provider request id; the span's duration is the call's
    latency. The span joins the run's one trace through :func:`resolve_trace_context` (the
    invoke config's explicit lineage, else the run's ambient deposit, else a fresh root for a
    standalone call) — the same lineage rule every feature's model calls follow. Emission
    goes through the contract writer, which is fail-safe; a provider failure records the span
    as an error and propagates unchanged, never swallowed. The inner pipeline still runs under
    the caller's config, so its framework step record is unaffected.

    The added work is one lineage resolve and one span open/update per call — constant and
    dominated by the provider's network round trip, so the hot path carries no avoidable cost.
    """

    def _record(response: ClassifyResponse, span: Any) -> None:
        span.update(
            model=response.model,
            usage=_usage(response.usage),
            metadata={"request_id": response.request_id} if response.request_id is not None else None,
        )

    def invoke(request: ClassifyRequest, config: RunnableConfig | None = None) -> ClassifyResponse:
        writer = tai42_app.monitoring.active.writer
        context = resolve_trace_context(dict(config) if config is not None else None)
        with writer.start_span(name="classifier", kind=SpanKind.LLM, trace_context=context) as span:
            try:
                response = pipeline.invoke(request, config)
            except Exception as exc:
                span.update(level=MonitoringLevel.ERROR, status_message=str(exc))
                raise
            _record(response, span)
            return response

    async def ainvoke(request: ClassifyRequest, config: RunnableConfig | None = None) -> ClassifyResponse:
        writer = tai42_app.monitoring.active.writer
        context = resolve_trace_context(dict(config) if config is not None else None)
        with writer.start_span(name="classifier", kind=SpanKind.LLM, trace_context=context) as span:
            try:
                response = await pipeline.ainvoke(request, config)
            except Exception as exc:
                span.update(level=MonitoringLevel.ERROR, status_message=str(exc))
                raise
            _record(response, span)
            return response

    return RunnableLambda(invoke, afunc=ainvoke, name="classifier")


def _build_classifier(provider: str, **kwargs) -> Runnable[ClassifyRequest, ClassifyResponse]:
    match provider:
        case "typesafe":
            from langchain_typesafe import ClassifierRequest, ClassifierResponse, Question, TypeSafeClassifier
            from langchain_typesafe.types import State

            # Built once per build and captured by the request lambda. Validating each
            # kit question's JSON dump through the vendor's own question union produces
            # the vendor question objects and raises loudly on a malformed question.
            questions_adapter: TypeAdapter[dict[str, Question]] = TypeAdapter(dict[str, Question])

            def _to_vendor_request(request: ClassifyRequest) -> ClassifierRequest:
                # The contract's ``state`` is any JSON value; the vendor's ``State`` is
                # narrower (no bare non-string scalar at the root) and rejects an
                # unsupported root value loudly at call time.
                return ClassifierRequest(
                    state=cast(State, request.state),
                    questions=questions_adapter.validate_python(
                        {
                            key: question.model_dump(mode="json", exclude_none=True)
                            for key, question in request.questions.items()
                        }
                    ),
                )

            def _to_kit_response(response: ClassifierResponse) -> ClassifyResponse:
                return ClassifyResponse.model_validate(response.model_dump(mode="json"))

            pipeline = (
                RunnableLambda(_to_vendor_request) | TypeSafeClassifier(**kwargs) | RunnableLambda(_to_kit_response)
            )
            return _recording_classifier(pipeline)

    raise ValueError(f"Unsupported classifier provider: '{provider}'")
