"""``register_hook``'s projected MCP tool form carries the jq annotations on its
FLAT ``condition`` / ``expr`` parameters.

The operation exposes flat ``condition``/``expr`` string params, so the model-level
``x-tai42-expression`` annotation on ``HookRegister.condition``/``expr`` never reaches
the tool form fastmcp derives from the flat signature — the door this suite guards.
The Annotated metadata on the two params restores it, stating the HOOK surface's
facts (both run over the webhook body; the condition is TRUTHY, unlike access
control's strict-true; the expr yields the fired tool's kwargs object). The tool
schema is derived exactly as the platform projects it (``_make_tool`` +
``Tool.from_function``), and the payloads are pinned VERBATIM as the wire shape a
RunPanel/schema-listing client reads.
"""

from __future__ import annotations

import json

from fastmcp.tools import Tool
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

from tai42_skeleton.operations.decorator import operation_metadata_of
from tai42_skeleton.operations.hooks import register_hook
from tai42_skeleton.operations.projection import _make_tool

# The hook-surface payloads, pinned VERBATIM: this is what a schema consumer reads
# off the projected register_hook tool.
HOOK_CONDITION_PAYLOAD = {
    "language": "jq",
    "label": "condition",
    "blurb": "the parsed webhook delivery (structured bodies merged top-level with query "
    "params; arbitrary text under raw_body)",
    "returns": "truthy to fire the hook; a falsy result skips it",
    "caveats": [
        "an absent/empty condition always fires; a present condition that "
        "produces no output errors the fire loudly (it is not a silent skip)"
    ],
}

HOOK_EXPR_PAYLOAD = {
    "language": "jq",
    "label": "expression",
    "blurb": "the parsed webhook delivery (structured bodies merged top-level with query "
    "params; arbitrary text under raw_body)",
    "returns": "an object — the fired tool's kwargs (the hook's static tool_kwargs win on a key clash)",
    "caveats": [
        "an absent/empty expression fires the tool with its static tool_kwargs only; "
        "a present expression must yield an object and errors the fire if it produces no output"
    ],
}


def _projected_tool_properties() -> dict:
    # Derive the schema the way the platform lists it: the projected wrapper copies
    # register_hook's flat signature (eval_str resolved), and fastmcp builds the
    # tool schema off that.
    tool = _make_tool(operation_metadata_of(register_hook))
    return Tool.from_function(tool).parameters["properties"]


def test_register_hook_projection_annotates_the_flat_jq_params() -> None:
    props = _projected_tool_properties()
    assert props["condition"][EXPRESSION_ANNOTATION_KEY] == HOOK_CONDITION_PAYLOAD
    assert props["expr"][EXPRESSION_ANNOTATION_KEY] == HOOK_EXPR_PAYLOAD


def test_annotation_stays_confined_to_the_two_jq_strings() -> None:
    props = _projected_tool_properties()
    # The template companions and every other flat param stay unannotated.
    for name in (
        "name",
        "topic",
        "tool",
        "execution_key",
        "tool_kwargs",
        "condition_id",
        "condition_kwargs",
        "expr_id",
        "expr_kwargs",
    ):
        assert EXPRESSION_ANNOTATION_KEY not in props[name], name


def test_annotation_is_purely_additive_to_the_flat_params() -> None:
    # Removing the vendor key must leave the exact schema an un-annotated
    # ``str | None = None`` param generates — proving the Annotated metadata adds
    # one key and changes nothing else (type, nullability, default).
    props = _projected_tool_properties()
    plain = json.dumps(props["condition_id"], sort_keys=True)
    for field in ("condition", "expr"):
        stripped = {k: v for k, v in props[field].items() if k != EXPRESSION_ANNOTATION_KEY}
        assert json.dumps(stripped, sort_keys=True) == plain


def test_projected_schema_stays_plain_json() -> None:
    # The whole listed schema is JSON-serializable — the shape every schema-listing
    # door emits.
    params = Tool.from_function(_make_tool(operation_metadata_of(register_hook))).parameters
    assert json.loads(json.dumps(params)) == params
