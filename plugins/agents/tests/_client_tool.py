"""A test stand-in for the platform's client-tool handle.

``get_client_tools`` hands an agent framework-neutral :class:`~tai42_contract.tools.ClientTool`
handles; the recording app double serves these instead. ``invoke`` calls the body with the
arguments as keywords and returns its value; whatever the body raises leaves ``invoke`` as it is
(a body that wants the model to see a failure raises ``ClientToolError`` itself, as the platform
handle does for a failing body).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import create_model


@dataclass(frozen=True)
class FakeClientTool:
    """A :class:`~tai42_contract.tools.ClientTool` over a plain body."""

    name: str
    description: str
    input_schema: dict[str, Any]
    body: Callable[..., Any] = field(repr=False)
    calls: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)

    async def invoke(self, arguments: Mapping[str, Any]) -> Any:
        self.calls.append(dict(arguments))
        result = self.body(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return result


def _inferred_schema(body: Callable[..., Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(body).parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = Any if param.annotation is inspect.Parameter.empty else param.annotation
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)
    schema = create_model("Arguments", **fields).model_json_schema()
    schema.pop("title", None)
    return schema


def client_tool(
    name: str,
    body: Callable[..., Any] | None = None,
    *,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    description: str | None = None,
) -> FakeClientTool:
    """A handle named ``name`` over ``body`` (an echo of its arguments when omitted).

    The argument schema is ``properties``/``required`` when given, else inferred from ``body``'s
    signature.
    """

    async def echo(**arguments: Any) -> Any:
        return arguments

    fn = body or echo
    if properties is not None or body is None:
        schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "required": required or []}
    else:
        schema = _inferred_schema(fn)
    return FakeClientTool(name=name, description=description or name, input_schema=schema, body=fn)
