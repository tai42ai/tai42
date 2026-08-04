"""The ``batch`` tool extension (TRANSFORMER kind).

Branches a tool into a ``<tool>_batch`` variant that runs many instances of the wrapped tool
from a list of input parameter sets, sequentially or in parallel, returning results in input
order. The variant presents its own composed signature: a ``params`` list typed by a generated
per-tool input model, plus ``execution_mode``, ``max_concurrent``, and ``fail_fast`` controls.
"""

import inspect
import sys
from typing import Any, Literal

from makefun import create_function
from pydantic import create_model
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.data import makefun_func_name, snake_to_pascal

from tai42_toolbox._internal.extensions.batch_executor import execute_batch


@tai42_app.extensions.extension(kind=ExtensionKind.TRANSFORMER, name="batch")
def batch(func, orig_name, orig_desc):
    """Branch ``func`` into a batched ``<orig_name>_batch`` variant."""
    type_hints = inspect.get_annotations(func, eval_str=True)
    sig = inspect.signature(func)

    # Per-tool pydantic input model so each batch item validates against the tool's own schema.
    input_fields: dict[str, Any] = {}
    for key, param in sig.parameters.items():
        field_type = type_hints.get(key, Any)
        if param.default is not param.empty:
            input_fields[key] = (field_type, param.default)
        else:
            input_fields[key] = (field_type, ...)

    input_name = f"{snake_to_pascal(orig_name)}Input"
    input_class = create_model(input_name, **input_fields, __module__=func.__module__)

    # Expose the model in the tool's module so its schema ``$ref`` resolves by qualified name.
    module = sys.modules[func.__module__]
    setattr(module, input_name, input_class)

    raw_name = f"{orig_name}_{batch.__name__}"
    safe_name = makefun_func_name(raw_name)

    async def func_impl(*args: Any, **kwargs: Any):
        params: list[Any] = kwargs["params"]
        return await execute_batch(
            orig_name,
            [param.model_dump() for param in params],
            execution_mode=kwargs["execution_mode"],
            max_concurrent=kwargs["max_concurrent"],
            fail_fast=kwargs["fail_fast"],
        )

    composed_sig = inspect.Signature(
        parameters=[
            inspect.Parameter("params", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=list[input_class]),
            inspect.Parameter(
                "execution_mode",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default="sequential",
                annotation=Literal["sequential", "parallel"],
            ),
            inspect.Parameter(
                "max_concurrent", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation=int | None
            ),
            inspect.Parameter("fail_fast", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=True, annotation=bool),
        ],
        return_annotation=list[Any],
    )

    description = f"""Batch extension for '{orig_name}'.
Runs multiple instances of the original tool sequentially or in parallel.

Args:
    params: list[{input_name}] — Input parameters for each run.
    execution_mode: 'sequential' or 'parallel'. Defaults to 'sequential'.
    max_concurrent: Limit concurrent tasks in parallel mode.
    fail_fast: Stop on first error if True; otherwise continue.

Returns:
    List of results in input order.

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
