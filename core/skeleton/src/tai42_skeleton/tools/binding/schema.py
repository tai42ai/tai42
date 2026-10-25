"""Derives a callable's presented input/output JSON schema and its concrete-annotation signature."""

import inspect
import keyword
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastmcp.tools.function_parsing import ParsedFunction
from fastmcp.utilities.types import get_cached_typeadapter
from pydantic import WithJsonSchema
from tai42_kit.utils.lc import resolved_type_hints
from tai42_kit.utils.unset import UNSET

from tai42_skeleton.tools.binding.errors import SchemaSignatureError

_VAR_PARAM_KINDS = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)

# The root keywords a signature can carry: the rest (combinators, ``patternProperties``,
# ``$defs``, ...) constrain the object in ways named arguments cannot present.
_SIGNATURE_ROOT_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "title",
        "description",
        "$schema",
        "$comment",
        "examples",
    }
)


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


def schema_signature(schema: Mapping[str, Any]) -> inspect.Signature:
    """A keyword-only signature presenting ``schema``'s properties as named arguments, in property order.

    Each parameter is annotated with its property subschema verbatim, so the input schema derived
    from the signature is ``schema``'s properties, ``required`` and ``additionalProperties: false``.
    A required name has no default; every other one defaults to
    :data:`~tai42_kit.utils.unset.UNSET`. A schema that named arguments cannot present raises
    :class:`~tai42_skeleton.tools.binding.errors.SchemaSignatureError`.
    """
    _check_signature_root(schema)
    properties: Mapping[str, Any] = schema.get("properties", {})
    required = list(schema.get("required", []))
    for name in required:
        if name not in properties:
            raise SchemaSignatureError(f"required name {name!r} is not a property")
    return inspect.Signature(
        [_property_parameter(name, subschema, required=name in required) for name, subschema in properties.items()]
    )


def _check_signature_root(schema: Mapping[str, Any]) -> None:
    """Raise :class:`SchemaSignatureError` unless ``schema``'s root is a closed object named arguments can present."""
    if schema.get("type") != "object":
        raise SchemaSignatureError('the schema root must be "type": "object"')
    if schema.get("additionalProperties") is not False:
        raise SchemaSignatureError('the schema must set "additionalProperties": false')
    for key in schema:
        if key not in _SIGNATURE_ROOT_KEYWORDS:
            raise SchemaSignatureError(f"the schema root keyword {key!r} cannot be presented as named arguments")


def _property_parameter(name: str, subschema: Any, *, required: bool) -> inspect.Parameter:
    """The keyword-only parameter presenting property ``name``; raise :class:`SchemaSignatureError` when it cannot."""
    if not isinstance(subschema, dict):
        raise SchemaSignatureError(f"property {name!r} must hold an object schema")
    if _holds_ref(subschema):
        raise SchemaSignatureError(f"property {name!r} holds a $ref")
    if not name.isidentifier() or keyword.iskeyword(name):
        raise SchemaSignatureError(f"property name {name!r} is not a valid parameter name")
    annotation = Annotated[Any, WithJsonSchema(subschema)]
    default = inspect.Parameter.empty if required else UNSET
    return inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default)


def _holds_ref(node: Any) -> bool:
    if isinstance(node, dict):
        return "$ref" in node or any(_holds_ref(value) for value in node.values())
    if isinstance(node, list):
        return any(_holds_ref(value) for value in node)
    return False


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

    A tool declared under ``from __future__ import annotations`` carries its return and
    parameter annotations as STRING forward-refs (e.g. ``"ExecResult"``). A makefun-built
    wrapper carrying that raw signature — a branch's runner of a transformed tool, or the
    dispatch validation wrapper — runs in a namespace whose globals do not contain those names, so a downstream
    pydantic ``TypeAdapter`` (schema parse or argument validation) evaluates the string in
    the wrong namespace and raises a bare ``NameError``. Resolving the hints here against the
    ORIGINAL function's own module globals turns the strings into real types the wrapper can
    carry in any namespace — :func:`~tai42_kit.utils.lc.resolved_type_hints` evaluates them in
    the module ``func.__module__`` names, which for a makefun extension branch is the wrapped
    tool's. An unannotated parameter keeps its empty annotation. An unresolvable hint raises
    loudly.
    """
    hints = resolved_type_hints(func)
    signature = inspect.signature(func)
    parameters = [
        param.replace(annotation=hints.get(param.name, param.annotation)) for param in signature.parameters.values()
    ]
    return signature.replace(
        parameters=parameters,
        return_annotation=hints.get("return", signature.return_annotation),
    )
