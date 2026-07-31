import inspect
from collections.abc import Callable
from typing import Any

from fastmcp.utilities.types import find_kwarg_by_type


def add_signature_params(
    func: Callable,
    additional_opts: dict[str, Any],
    exclude_fastmcp_ctx: bool = False,
) -> inspect.Signature:
    original_sig = inspect.signature(func)
    additional_params = [
        inspect.Parameter(
            name=key,
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=annotation,
        )
        for key, annotation in additional_opts.items()
    ]

    modified_original_params = []
    if not exclude_fastmcp_ctx:
        modified_original_params = list(original_sig.parameters.values())
    else:
        # Loop-invariant: compute the Context kwarg once, not per parameter.
        from fastmcp import Context

        context_kwarg = find_kwarg_by_type(func, kwarg_type=Context)
        for param in original_sig.parameters.values():
            if param.name == context_kwarg:
                param = param.replace(annotation=Any)
            modified_original_params.append(param)

    # A VAR_KEYWORD (**kwargs) parameter must stay last in a signature, so the
    # added keyword-only params go before it — appending after would raise
    # ValueError ("wrong parameter order").
    if modified_original_params and modified_original_params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        *head, var_keyword = modified_original_params
        new_params = [*head, *additional_params, var_keyword]
    else:
        new_params = modified_original_params + additional_params
    return original_sig.replace(parameters=new_params)


def exclude_fastmcp_ctx_from_kwargs(func: Callable, arguments: dict[str, Any]) -> dict[str, Any]:
    from fastmcp import Context

    context_kwarg = find_kwarg_by_type(func, kwarg_type=Context)
    if context_kwarg and context_kwarg in arguments:
        arguments = {k: v for k, v in arguments.items() if k != context_kwarg}
    return arguments
