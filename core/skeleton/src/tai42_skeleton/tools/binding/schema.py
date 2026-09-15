"""Derives a callable's presented input/output JSON schema and its concrete-annotation signature."""

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from fastmcp.tools.function_parsing import ParsedFunction
from fastmcp.utilities.types import get_cached_typeadapter

_VAR_PARAM_KINDS = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)


def _derive_input_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """JSON schema of ``func``'s PRESENTED signature.

    Derived via ``get_cached_typeadapter`` (the adapter fastmcp builds tools on), which
    follows ``__signature__`` / ``__wrapped__``. This is the RAW presented-signature schema
    — deliberately WITHOUT fastmcp's exposure-time post-processing (injected
    Context/``Depends`` stripping, title removal): the wrapper identity check compares the
    full declared input contract. Never inspects the raw implementation body — extensions
    legitimately implement with ``*args/**kwargs`` behind a concrete makefun-presented
    signature, and reading the impl would wrongly reject them.
    """
    return get_cached_typeadapter(func).json_schema()


def _drop_empty_required(schema: dict[str, Any]) -> None:
    """Normalize an empty ``required`` list to no key at all.

    Removing a default-less reserved param can empty ``required``, and a baseline with no
    required params carries no ``required`` key — they must compare equal.
    """
    if schema.get("required") == []:
        del schema["required"]


def _derive_output_schema(func: Callable[..., Any]) -> dict[str, Any] | None:
    """The tool OUTPUT schema FastMCP would derive from ``func``'s return type.

    ``None`` when the return is unannotated. A non-object return comes back as a WRAPPED
    ``{... "x-fastmcp-wrap-result": true}`` object schema. Derived from the FUNCTION (not a
    registered ``Tool``) because branch tools are bound BEFORE the base, so the base is not
    yet a registered tool at propagation time.

    A body presenting a bare ``**kwargs`` (e.g. a synthesized agent run tool,
    whose typed contract lives on its explicit ``parameters`` rather than its
    signature) has no schema FastMCP can parse from the function, so there is no
    output schema to derive or propagate — return ``None``.
    """
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in inspect.signature(func).parameters.values()):
        return None
    return ParsedFunction.from_function(func).output_schema


def _declares_own_output_schema(func: Callable[..., Any]) -> bool:
    """Whether ``func`` genuinely declares its OWN object output schema.

    A real object return, not the auto-wrapped placeholder for a non-object/absent return. A
    branch that declares its own KEEPS it; only a branch that declares none may inherit the
    base tool's output schema.
    """
    schema = _derive_output_schema(func)
    return schema is not None and not schema.get("x-fastmcp-wrap-result")


def _resolved_signature(func: Callable[..., Any]) -> inspect.Signature:
    """``func``'s signature with every annotation resolved to a CONCRETE type.

    A base tool declared under ``from __future__ import annotations`` carries its
    return and parameter annotations as STRING forward-refs (e.g. ``"ExecResult"``).
    When ``_baked_partial`` copies that raw signature onto a makefun-built partial, the
    partial's globals do not contain those names, so the downstream schema parse
    (``_derive_output_schema`` → pydantic's ``TypeAdapter``) evaluates the string in the
    wrong namespace and raises a bare ``NameError``. Resolving the hints here against the
    ORIGINAL function's own module globals turns the strings into real types the partial
    can advertise in any namespace. ``include_extras`` keeps ``Annotated`` metadata; an
    unannotated parameter keeps its empty annotation. An unresolvable hint raises loudly.
    """
    hints = get_type_hints(func, include_extras=True)
    signature = inspect.signature(func)
    parameters = [
        param.replace(annotation=hints.get(param.name, param.annotation)) for param in signature.parameters.values()
    ]
    return signature.replace(
        parameters=parameters,
        return_annotation=hints.get("return", signature.return_annotation),
    )
