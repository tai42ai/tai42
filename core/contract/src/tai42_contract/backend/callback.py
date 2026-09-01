from __future__ import annotations

from typing import Annotated

from pydantic import Field

from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, ConditionMixin, ExprMixin, expression_annotation


class CallbackSchema(ConditionMixin, ExprMixin):
    # Optional: with no ``tool`` the backend runs the rendered ``expr`` directly.
    tool: str = ""

    # The mixins' generic jq annotations, refined with this surface's facts. BOTH
    # expressions evaluate over the finished backend task's raw tool result — an
    # untyped document (a tool may return any JSON shape), hence ``keys=[]`` and
    # the free-form ``{}`` sample. The runtime facts mirrored here live in the
    # kit-side ``callback_execution``: a falsy condition output skips the
    # callback; the expr output is handed to the follow-up tool as its kwargs,
    # and an empty expression — or a jq pipeline that produces no output —
    # deliberately yields ``{}`` rather than an error.
    condition: Annotated[
        str | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="condition",
                    blurb="the backend task's tool result",
                    keys=[],
                    returns="truthy to run the callback",
                    sample={},
                )
            }
        ),
    ] = None
    expr: Annotated[
        str | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="expression",
                    blurb="the backend task's tool result",
                    keys=[],
                    returns="an object — the follow-up tool's kwargs",
                    caveats=[
                        "an empty expression, or a jq pipeline producing no output, "
                        "yields {} (the follow-up tool runs with empty kwargs) — not an error"
                    ],
                    sample={},
                )
            }
        ),
    ] = None
