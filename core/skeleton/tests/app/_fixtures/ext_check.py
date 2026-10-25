"""Fixture extensions for the dry-run combo check, over the base tool ``echo`` of
:mod:`tests.app._fixtures.tools_check`.

``refusing`` refuses the setting ``label == "x"`` (and raises with an empty message on
``"blank"``); ``labelled`` and ``labelled_n`` declare required settings; ``sameret``
returns its input unchanged (a same-name return); ``localw`` requires body locality;
``relocx`` and ``relocy`` are two relocating backends. Every factory call is recorded
in :data:`CALLS` by extension name.
"""

import functools
from collections.abc import Callable
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind

CALLS: list[str] = []


def _renamed(func: Callable[..., Any], name: str, suffix: str) -> Callable[..., Any]:
    @functools.wraps(func)
    def variant(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    variant.__name__ = f"{name}_{suffix}"
    variant.__qualname__ = variant.__name__
    return variant


@tai42_app.extensions.extension(
    kind=ExtensionKind.WRAPPER,
    name="refusing",
    config_schema={"type": "object", "properties": {"label": {"type": "string"}}, "additionalProperties": False},
)
def refusing(func, name, desc, config):
    CALLS.append("refusing")
    if config.get("label") == "x":
        raise ValueError("bad label")
    if config.get("label") == "blank":
        raise ValueError
    return _renamed(func, name, "refusing")


@tai42_app.extensions.extension(
    kind=ExtensionKind.WRAPPER,
    name="labelled",
    config_schema={
        "type": "object",
        "properties": {"label": {"type": "string", "minLength": 1}},
        "required": ["label"],
        "additionalProperties": False,
    },
)
def labelled(func, name, desc, config):
    CALLS.append("labelled")
    return _renamed(func, name, "labelled")


@tai42_app.extensions.extension(
    kind=ExtensionKind.WRAPPER,
    name="labelled_n",
    config_schema={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
)
def labelled_n(func, name, desc, config):
    CALLS.append("labelled_n")
    return _renamed(func, name, "labelled_n")


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="sameret")
def sameret(func, name, desc):
    CALLS.append("sameret")
    return func


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="localw", requires_body_locality=True)
def localw(func, name, desc):
    CALLS.append("localw")
    return _renamed(func, name, "localw")


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="relocx")
def relocx(func, name, desc):
    CALLS.append("relocx")
    return _renamed(func, name, "relocx")


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="relocy")
def relocy(func, name, desc):
    CALLS.append("relocy")
    return _renamed(func, name, "relocy")
