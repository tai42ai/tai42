import json
from typing import Any

from langchain_core.messages import SystemMessage
from pydantic import BaseModel

from tai42_kit.utils.data.json_schema_util import validate_against_json_schema


def build_agent_input(*user_messages: str) -> dict[str, Any]:
    """Build a LangGraph agent input (``{"messages": [...]}``) from raw text.

    Appends each of *user_messages* as a user turn, coercing each to ``str``.
    The system prompt is deliberately NOT part of the input: agent input becomes
    checkpointed conversation state, and the system prompt is per-run model
    configuration — build it with :func:`build_system_message` and hand it to the
    graph factory (``create_agent(system_prompt=...)``) so it is applied at the
    model-call boundary instead of persisted into thread history.
    """
    return {"messages": [{"role": "user", "content": str(content)} for content in user_messages]}


def build_system_message(
    system_message: str | None,
    system_content_kwargs: dict[str, Any] | None = None,
) -> SystemMessage | None:
    """Build the per-run system prompt as a ``SystemMessage``, or ``None`` for an
    empty one.

    With *system_content_kwargs* (e.g. ``cache_control`` for prompt caching) the
    content is a single structured text block carrying those keys; otherwise it is
    the plain string. Hand the result to the graph factory
    (``create_agent(system_prompt=...)``), never into the input messages — the
    system prompt is per-run configuration and must stay out of checkpointed
    thread state.
    """
    if not system_message:
        return None
    if system_content_kwargs:
        return SystemMessage(content=[{"type": "text", "text": system_message, **system_content_kwargs}])
    return SystemMessage(content=system_message)


def build_user_output(state: dict[str, Any]) -> str:
    """Extract the final message's text from an agent *state* as a plain string.

    Returns the last message's ``content`` when it is a string; for list content,
    joins string parts, or pulls the ``text``/``content`` key from each dict part
    (stringifying unknown parts), serializing mixed content to JSON. Returns
    ``""`` when there is no final message or it has no ``content``.
    """
    messages = state.get("messages", [])
    if not messages:
        return ""

    final_message = messages[-1]
    if not hasattr(final_message, "content"):
        return ""

    content = final_message.content
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        if all(isinstance(item, str) for item in content):
            return "\n".join(content)
        elif all(isinstance(item, dict) for item in content):
            # Pull the text out of each content dict, trying the common keys in order.
            texts = []
            for item in content:
                if "text" in item:
                    texts.append(item["text"])
                elif "content" in item:
                    texts.append(item["content"])
                else:
                    # No known text key; stringify the whole item.
                    texts.append(str(item))
            return "\n".join(texts)
        else:
            # Mixed types: serialize to JSON
            return json.dumps(content)
    else:
        # Unknown type: coerce to string
        return str(content)


def validate_structured_output(structured: Any, response_format: Any) -> Any:
    """Validate a produced structured output against the ``response_format`` that
    forced it.

    * a pydantic model class → ``model_validate`` the produced value and return
      the validated instance;
    * a JSON-Schema ``dict`` → validate the raw value (a pydantic instance is
      dumped to JSON-native types first, so e.g. a ``datetime`` field validates
      as its ISO string) with the faithful draft-2020-12 validator — every
      constraint keyword enforced — and return the value as produced;
    * anything else (e.g. a langchain response strategy) → returned as produced.

    A value that does not match raises loudly (``pydantic.ValidationError`` /
    :class:`~tai42_kit.utils.data.json_schema_util.JsonSchemaValidationError`) —
    never a silent pass-through of a non-conforming object.
    """
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return response_format.model_validate(structured)
    if isinstance(response_format, dict):
        instance = structured.model_dump(mode="json") if isinstance(structured, BaseModel) else structured
        validate_against_json_schema(instance, response_format)
        return structured
    return structured


def extract_structured_output(state: dict[str, Any], response_format: Any) -> Any:
    """Extract and validate an agent's forced structured output from *state*.

    Reads ``state['structured_response']`` — the value a langgraph agent built
    with a ``response_format`` writes — and returns it validated via
    :func:`validate_structured_output`.

    Raises ``RuntimeError`` when a structured output was requested but the agent
    produced none (missing/``None`` in the final state) — the caller opted into
    a structured object, so a missing one is a failure, not a silent ``None``.
    """
    structured = state.get("structured_response")
    if structured is None:
        raise RuntimeError(
            "response_format was requested but the agent produced no structured_response (missing/None in final state)."
        )
    return validate_structured_output(structured, response_format)
