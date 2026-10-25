"""Where the input a run of a tool validates can hold a ``TemplatedText``, and the stored ids it names.

The annotations are the ones the run validates against: a registered agent's ``ToolInput``
fields, constraints included, else the resolved signature of a registered ``FunctionTool``
(an extension branch's own included). A tool with no Python input to read, or a name the
registry does not serve, answers ``None`` and names no id.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated, Any, Union, get_args, get_origin

from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools.function_tool import FunctionTool
from pydantic import BaseModel, TypeAdapter, ValidationError
from tai42_contract.template import TemplatedText

from tai42_skeleton.app import instance
from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.schema import _VAR_PARAM_KINDS, _resolved_signature

_SEQUENCE_ORIGINS = frozenset({list, tuple, set, frozenset, Sequence})
_MAPPING_ORIGINS = frozenset({dict, Mapping})


async def tool_input_annotations(tool: str) -> Mapping[str, Any] | None:
    """The annotation, constraints included, that a run of ``tool`` validates each input field against.

    ``None`` for a tool with no Python input model to read.
    """
    agent = instance.app.agents.all_agents().get(tool)
    if agent is not None:
        annotations: dict[str, Any] = {}
        for name, field in agent.ToolInput.model_fields.items():
            annotation: Any = field.annotation
            for meta in field.metadata:
                annotation = Annotated[annotation, meta]
            annotations[name] = annotation
        return annotations
    try:
        bound = await instance.app.tools.get_tool(tool)
    except UnknownToolError:
        return None
    if not isinstance(bound, FunctionTool):
        return None
    signature = _resolved_signature(without_injected_parameters(bound.fn))
    return {
        name: param.annotation
        for name, param in signature.parameters.items()
        if param.kind not in _VAR_PARAM_KINDS and param.annotation is not param.empty
    }


def _can_hold(annotation: Any, seen: set[type[BaseModel]]) -> bool:
    """Whether a value of ``annotation`` can hold a ``TemplatedText`` at any depth."""
    if isinstance(annotation, type) and issubclass(annotation, TemplatedText):
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return _can_hold(args[0], seen)
    if origin is Union or origin is types.UnionType:
        return any(_can_hold(arg, seen) for arg in args)
    if origin in _SEQUENCE_ORIGINS:
        return any(_can_hold(arg, seen) for arg in args if arg is not Ellipsis)
    if origin in _MAPPING_ORIGINS:
        return len(args) == 2 and _can_hold(args[1], seen)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return False
        seen.add(annotation)
        return any(_can_hold(field.annotation, seen) for field in annotation.model_fields.values())
    return False


def _stored_ids(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, TemplatedText):
        if value.id is not None:
            yield path, value.id
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _stored_ids(getattr(value, name), f"{path}.{name}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _stored_ids(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _stored_ids(item, f"{path}.{key}")


def _error_text(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(part) for part in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts)


async def annotated_template_ids(tool: str, kwargs: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(path, id)`` of every ``TemplatedText`` given by ``id`` in ``kwargs``, in field then document order.

    Reads only the fields whose annotation can hold a ``TemplatedText``; raises ``ValueError`` naming the
    field, quoting no value, for a value that annotation refuses.
    """
    annotations = await tool_input_annotations(tool)
    if annotations is None:
        return []
    ids: list[tuple[str, str]] = []
    for name, annotation in annotations.items():
        if name not in kwargs or not _can_hold(annotation, set()):
            continue
        try:
            value = TypeAdapter(annotation).validate_python(kwargs[name])
        except ValidationError as exc:
            raise ValueError(f"{name} is not a valid value for tool {tool!r}: {_error_text(exc)}") from exc
        ids.extend(_stored_ids(value, name))
    return ids


__all__ = ["annotated_template_ids", "tool_input_annotations"]
