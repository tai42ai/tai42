"""The span attribute schema every record is written in.

GenAI names are the OpenTelemetry GenAI semantic conventions as of
``opentelemetry-semantic-conventions`` 0.65b0 (incubating); chain and event payloads use
the OpenInference ``input.value`` / ``output.value`` names; everything platform-specific is
under ``tai42.``. Backend vocabulary is mapped by the deployment's collector, never here.
"""

from __future__ import annotations

from tai42_contract.monitoring import SpanKind

# The record's neutral kind, always set.
SPAN_KIND = "tai42.span.kind"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_OPERATION_CHAT = "chat"
GEN_AI_OPERATION_EXECUTE_TOOL = "execute_tool"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"

MODEL_PARAMETERS = "tai42.model_parameters"
USAGE_TOTAL_TOKENS = "tai42.usage.total_tokens"
USAGE_COST_USD = "tai42.usage.cost_usd"
LEVEL = "tai42.level"
STATUS_MESSAGE = "tai42.status_message"
# Every non-promoted producer metadata key, as ONE JSON object.
METADATA = "tai42.metadata"

TRACE_NAME = "tai42.trace.name"
TRACE_TAGS = "tai42.trace.tags"
TRACE_METADATA = "tai42.trace.metadata"
RUN_VERSION = "tai42.run.version"
USER_ID = "user.id"
SESSION_ID = "session.id"

DEPLOYMENT_ENVIRONMENT_NAME = "deployment.environment.name"

_INPUT_ATTRIBUTE: dict[SpanKind, str] = {
    SpanKind.LLM: GEN_AI_INPUT_MESSAGES,
    SpanKind.TOOL: GEN_AI_TOOL_CALL_ARGUMENTS,
    SpanKind.CHAIN: INPUT_VALUE,
    SpanKind.EVENT: INPUT_VALUE,
}
_OUTPUT_ATTRIBUTE: dict[SpanKind, str] = {
    SpanKind.LLM: GEN_AI_OUTPUT_MESSAGES,
    SpanKind.TOOL: GEN_AI_TOOL_CALL_RESULT,
    SpanKind.CHAIN: OUTPUT_VALUE,
    SpanKind.EVENT: OUTPUT_VALUE,
}


def input_attribute(kind: SpanKind) -> str:
    """The attribute a record of ``kind`` carries its input in."""
    return _INPUT_ATTRIBUTE[kind]


def output_attribute(kind: SpanKind) -> str:
    """The attribute a record of ``kind`` carries its output in."""
    return _OUTPUT_ATTRIBUTE[kind]


def kind_attributes(kind: SpanKind, name: str) -> dict[str, str]:
    """The attributes every record of ``kind`` named ``name`` carries from its start."""
    attributes = {SPAN_KIND: kind.value}
    if kind is SpanKind.LLM:
        attributes[GEN_AI_OPERATION_NAME] = GEN_AI_OPERATION_CHAT
    elif kind is SpanKind.TOOL:
        attributes[GEN_AI_OPERATION_NAME] = GEN_AI_OPERATION_EXECUTE_TOOL
        attributes[GEN_AI_TOOL_NAME] = name
    return attributes
