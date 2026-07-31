import json
from typing import Any

from pydantic import BaseModel

from tai42_kit.utils.data.json_schema_util import validate_against_json_schema


def build_agent_input(
    *user_messages: str,
    role: str = "user",
    system_message: str | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a LangGraph agent input (``{"messages": [...]}``) from raw text.

    Prepends an optional system message (as structured content when
    *system_content_kwargs* is given, e.g. ``cache_control``, else a plain
    string), then appends each of *user_messages* under *role*, coercing each to
    ``str``.
    """
    messages = []
    if system_message:
        if system_content_kwargs:
            messages.append(
                {
                    "role": "system",
                    "content": [{"type": "text", "text": system_message, **system_content_kwargs}],
                }
            )
        else:
            messages.append({"role": "system", "content": system_message})

    for content in user_messages:
        messages.append({"role": role, "content": str(content)})

    return {"messages": messages}


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
