"""The dispatch branches' LISTED schemas carry the callback jq annotations
with NO backend-side code.

``callback_kwargs: CallbackSchema | None`` is typed in the branch signatures, so
the ``x-tai42-expression`` vendor annotation the contract declares on the
callback's ``condition``/``expr`` fields flows into the listed tool schema
automatically — the passthrough this suite proves. The platform lists a branch
tool's input schema by deriving it from the composed callable exactly as here
(FastMCP ``Tool.from_function``).
"""

from __future__ import annotations

import json

import pytest
from fastmcp.tools import Tool
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

import tai42_backend_celery.extensions.extensions as extensions


def sample_tool(a: int, b: str = "x") -> str:
    """A sample tool."""
    return f"{a}-{b}"


# The contract's callback payloads, pinned VERBATIM at the backend edge: this is
# the wire shape a schema consumer reads off a listed task tool.
CALLBACK_CONDITION_PAYLOAD = {
    "language": "jq",
    "label": "condition",
    "blurb": "the backend task's tool result",
    "keys": [],
    "returns": "truthy to run the callback",
    "sample": {},
}

CALLBACK_EXPR_PAYLOAD = {
    "language": "jq",
    "label": "expression",
    "blurb": "the backend task's tool result",
    "keys": [],
    "returns": "an object — the follow-up tool's kwargs",
    "caveats": [
        "an empty expression, or a jq pipeline producing no output, "
        "yields {} (the follow-up tool runs with empty kwargs) — not an error"
    ],
    "sample": {},
}


@pytest.mark.parametrize(
    ("factory", "suffix"),
    [(extensions.sync_task, "sync_task"), (extensions.async_task, "async_task")],
)
def test_dispatch_branch_listed_schema_annotates_the_callback_expressions(factory, suffix) -> None:
    branch = factory(sample_tool, "sample_tool", "doc")
    parameters = Tool.from_function(branch).parameters

    callback_def = parameters["$defs"]["CallbackSchema"]["properties"]
    assert callback_def["condition"][EXPRESSION_ANNOTATION_KEY] == CALLBACK_CONDITION_PAYLOAD
    assert callback_def["expr"][EXPRESSION_ANNOTATION_KEY] == CALLBACK_EXPR_PAYLOAD

    # The annotation stays confined to the two jq strings: the callback's other
    # fields and every dispatch option remain unannotated.
    for name in ("condition_id", "condition_kwargs", "expr_id", "expr_kwargs", "tool"):
        assert EXPRESSION_ANNOTATION_KEY not in callback_def[name]
    for name in ("queue", "countdown", "priority", "retry", "routing_key", "expires", "eta", "a", "b"):
        assert EXPRESSION_ANNOTATION_KEY not in parameters["properties"][name]

    # ``callback_kwargs`` itself is a $ref to the model, not a jq string — the
    # annotation rides inside the referenced definition only.
    assert EXPRESSION_ANNOTATION_KEY not in parameters["properties"]["callback_kwargs"]

    # The whole listed schema stays plain JSON — the shape every schema-listing
    # door serializes.
    assert json.loads(json.dumps(parameters)) == parameters


def test_unannotated_callback_string_field_is_byte_identical_to_a_plain_declaration() -> None:
    # The listed form has FastMCP's title pruning applied, so a plain string
    # field is exactly ``{"default": "", "type": "string"}`` — no vendor key, no
    # other drift.
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")
    tool_prop = Tool.from_function(branch).parameters["$defs"]["CallbackSchema"]["properties"]["tool"]
    assert json.dumps(tool_prop, sort_keys=True) == json.dumps({"default": "", "type": "string"}, sort_keys=True)
