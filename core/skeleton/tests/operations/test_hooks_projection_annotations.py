"""``register_hook``'s projected MCP tool form carries the jq annotations on its FLAT
``condition`` and the four door-contract expression parameters.

The operation exposes flat ``condition`` / ``start_expr`` / ``cancel_expr`` / ``resume_expr`` /
``extras_expr`` params, each a ``TemplatedText``, so the model-level ``x-tai42-expression`` annotation
never reaches the tool form fastmcp derives from the flat signature — the door this suite guards. The
Annotated metadata on the params restores it, stating the HOOK surface's facts (each runs over the
webhook body; the condition is TRUTHY; the door expressions read ``$parked``). The tool schema is
derived exactly as the platform projects it (``_make_tool`` + ``Tool.from_function``), and the
payloads are pinned as the wire shape a schema-listing client reads.
"""

from __future__ import annotations

import json

from fastmcp.tools import Tool
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation

from tai42_skeleton.operations.decorator import operation_metadata_of
from tai42_skeleton.operations.hooks import register_hook
from tai42_skeleton.operations.projection import _make_tool

_DELIVERY_BLURB = (
    "the parsed webhook delivery (structured bodies merged top-level with query params; arbitrary text under raw_body)"
)
_PARKED_VARIABLE = (
    "parked",
    "the run's currently parked interactions on the hook's subject — each with its id, status, to, "
    "asked_by, question and answer-format fields",
    [{"id": "i-42", "status": "asking", "to": "caller", "asked_by": ["main"], "answer_format": "confirm"}],
)

HOOK_CONDITION_PAYLOAD = {
    "language": "jq",
    "label": "condition",
    "blurb": _DELIVERY_BLURB,
    "returns": "truthy to fire the hook; a falsy result skips it",
    "caveats": [
        "an absent/empty condition always fires; a present condition that "
        "produces no output errors the fire loudly (it is not a silent skip)"
    ],
}

_DOOR_PAYLOADS = {
    "start_expr": expression_annotation(
        label="start expression",
        blurb=_DELIVERY_BLURB,
        variables=[_PARKED_VARIABLE],
        returns="an object — the fired tool's kwargs (the hook's static tool_kwargs win on a key clash)",
        caveats=[
            "absent fires the tool with its static tool_kwargs only; a present start expression "
            "must yield an object or null — null starts nothing (the run may still be "
            "cancelled/resumed by the other expressions)"
        ],
    ),
    "cancel_expr": expression_annotation(
        label="cancel expression",
        blurb=_DELIVERY_BLURB,
        variables=[_PARKED_VARIABLE],
        returns="null (cancel nothing), a parked interaction id, or a list of ids to cancel",
    ),
    "resume_expr": expression_annotation(
        label="resume expression",
        blurb=_DELIVERY_BLURB,
        variables=[_PARKED_VARIABLE],
        returns=(
            "null (resume nothing), {id, payload} to resume an ask with an answer, a bare id to "
            "take a waiting outcome, or a list of these"
        ),
    ),
    "extras_expr": expression_annotation(
        label="extras expression",
        blurb=_DELIVERY_BLURB,
        variables=[_PARKED_VARIABLE],
        returns="the extras mapping handed to the started target; null for no extras",
    ),
}

_JQ_PARAMS = ("condition", "start_expr", "cancel_expr", "resume_expr", "extras_expr")


def _projected_tool_properties() -> dict:
    tool = _make_tool(operation_metadata_of(register_hook))
    return Tool.from_function(tool).parameters["properties"]


def test_register_hook_projection_annotates_the_flat_jq_params() -> None:
    props = _projected_tool_properties()
    assert props["condition"][EXPRESSION_ANNOTATION_KEY] == HOOK_CONDITION_PAYLOAD
    for field, payload in _DOOR_PAYLOADS.items():
        assert props[field][EXPRESSION_ANNOTATION_KEY] == payload


def test_annotation_stays_confined_to_the_jq_params() -> None:
    props = _projected_tool_properties()
    for name in ("name", "topic", "tool", "execution_key", "tool_kwargs", "subject", "state_binding"):
        assert EXPRESSION_ANNOTATION_KEY not in props[name], name


def test_annotation_is_purely_additive_to_the_flat_params() -> None:
    # Removing the vendor key must leave the exact schema an un-annotated
    # ``TemplatedText | None = None`` param generates — proving the Annotated metadata
    # adds one key and changes nothing else (type, nullability, default).
    async def _plain(param: TemplatedText | None = None) -> None: ...

    plain = json.dumps(Tool.from_function(_plain).parameters["properties"]["param"], sort_keys=True)
    props = _projected_tool_properties()
    for field in _JQ_PARAMS:
        stripped = {k: v for k, v in props[field].items() if k != EXPRESSION_ANNOTATION_KEY}
        assert json.dumps(stripped, sort_keys=True) == plain


def test_projected_schema_stays_plain_json() -> None:
    params = Tool.from_function(_make_tool(operation_metadata_of(register_hook))).parameters
    assert json.loads(json.dumps(params)) == params
