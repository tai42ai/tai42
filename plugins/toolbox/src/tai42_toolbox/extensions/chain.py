"""The ``chain`` tool extension (TRANSFORMER kind).

Branches a tool into a ``<tool>_chain`` variant that calls the wrapped tool, transforms its
output with a jq expression, then calls a second named tool with the result. The variant presents
its own composed signature (the wrapped tool's parameters plus ``jq_expression`` and
``next_tool_name``). Needs the ``chain`` extra (jq); fails loudly at import when jq is absent.
"""

import inspect
from typing import Annotated, Any

from makefun import create_function
from pydantic import Field
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, expression_annotation
from tai42_kit.utils.data import makefun_func_name

from tai42_toolbox._internal.extensions.chain_executor import execute_chain
from tai42_toolbox._internal.extensions.signature import with_added_params

# The jq-typed parameter's schema annotation: the composed variant runs the
# expression over the wrapped tool's raw output and hands the result to the
# second tool as its kwargs object (see ``execute_chain``). Declared on the
# ``Annotated`` parameter type so the derived tool schema carries it; purely
# schema metadata, never validation.
_JQ_EXPRESSION_PARAM = Annotated[
    str,
    Field(
        json_schema_extra={
            EXPRESSION_ANNOTATION_KEY: expression_annotation(
                label="expression",
                blurb="the first tool's raw output",
                returns="the second tool's kwargs object",
            )
        }
    ),
]


@tai42_app.extensions.extension(kind=ExtensionKind.TRANSFORMER, name="chain")
def chain(func, orig_name, orig_desc):
    """Branch ``func`` into a chained ``<orig_name>_chain`` variant."""
    sig = inspect.signature(func)

    raw_name = f"{orig_name}_{chain.__name__}"
    safe_name = makefun_func_name(raw_name)

    # Keyword-only so the required controls can follow any defaulted parameter and arrive in ``kwargs``.
    composed_sig = with_added_params(
        sig.replace(return_annotation=Any),
        inspect.Parameter("jq_expression", inspect.Parameter.KEYWORD_ONLY, annotation=_JQ_EXPRESSION_PARAM),
        inspect.Parameter("next_tool_name", inspect.Parameter.KEYWORD_ONLY, annotation=str),
    )

    async def func_impl(*args: Any, **kwargs: Any):
        jq_expression = kwargs.pop("jq_expression")
        next_tool_name = kwargs.pop("next_tool_name")
        return await execute_chain(
            orig_name,
            kwargs,
            jq_expression=jq_expression,
            next_tool_name=next_tool_name,
        )

    description = f"""Chain extension for '{orig_name}'.
Calls the original tool, applies a jq expression to its output, then calls a second tool with the result.

Args:
    jq_expression: jq expression to transform the first tool's output into arguments for the next tool.
    next_tool_name: Name of the tool to call with the transformed output.

Original doc:
{orig_desc}
"""

    branch = create_function(
        func_signature=composed_sig,
        func_impl=func_impl,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=description,
    )
    # The branch loop registers a branch by its ``__name__``; restore the raw
    # intended name so a hyphenated name isn't collapsed to its makefun alias.
    branch.__name__ = raw_name
    return branch
