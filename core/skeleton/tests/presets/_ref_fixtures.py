"""Base tools whose ``tool_refs`` extractors answer each shape the reference check reads.

``sited_refs_tool`` yields a sited and an unsited entry; ``str_refs_tool`` answers a bare
string; ``keyerror_refs_tool`` raises ``KeyError``; ``shape_refs_tool`` refuses its blob with
a ``ValueError``; ``strict_refs_tool`` raises ``TypeError`` on a blob whose ``plan`` is not an
object, the body its write validator refuses.

``jq_refs_tool`` declares a ``template_refs`` extractor naming ``jq/missing`` at ``node 'fetch'
expr`` beside a ``TemplatedText`` parameter its extractor answers for; ``jq_shape_tool``'s
extractor refuses its blob with a ``ValueError``, ``jq_keyerror_tool``'s raises ``KeyError`` and
``jq_str_tool``'s answers a bare string.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_contract.tools import ComposedToolRef, TemplateRef


def _sited_refs(fixed_kwargs: dict[str, Any]) -> list[ComposedToolRef]:
    return [ComposedToolRef(name="ghost", site="step 'a'"), ComposedToolRef(name="absent")]


@tai42_app.tools.tool(tool_refs=_sited_refs)
def sited_refs_tool(text: str = "") -> str:
    """Composes ``ghost`` at step ``a`` and ``absent``."""
    return text


def _str_refs(fixed_kwargs: dict[str, Any]) -> list[Any]:
    return ["ghost"]


@tai42_app.tools.tool(tool_refs=_str_refs)
def str_refs_tool(text: str = "") -> str:
    """Its extractor answers a bare string entry."""
    return text


def _keyerror_refs(fixed_kwargs: dict[str, Any]) -> list[ComposedToolRef]:
    raise KeyError("steps")


@tai42_app.tools.tool(tool_refs=_keyerror_refs)
def keyerror_refs_tool(text: str = "") -> str:
    """Its extractor raises ``KeyError``."""
    return text


def _shape_refs(fixed_kwargs: dict[str, Any]) -> list[ComposedToolRef]:
    raise ValueError("plan must be a list")


@tai42_app.tools.tool(tool_refs=_shape_refs)
def shape_refs_tool(text: str = "") -> str:
    """Its extractor refuses every blob as off-shape."""
    return text


strict_refs_calls: list[dict[str, Any]] = []


def _strict_refs(fixed_kwargs: dict[str, Any]) -> list[ComposedToolRef]:
    strict_refs_calls.append(fixed_kwargs)
    plan = fixed_kwargs.get("plan", {})
    if not isinstance(plan, dict):
        raise TypeError("plan is not an object")
    return [ComposedToolRef(name=name) for name in plan.get("refs", [])]


@tai42_app.tools.tool(tool_refs=_strict_refs)
def strict_refs_tool(text: str = "", plan: Any = None) -> str:
    """Its extractor raises ``TypeError`` on a ``plan`` that is not an object."""
    return text


def _jq_refs(kwargs: dict[str, Any]) -> list[TemplateRef]:
    return [TemplateRef(id="jq/missing", site="node 'fetch' expr")]


@tai42_app.tools.tool(template_refs=_jq_refs)
def jq_refs_tool(text: str = "", prompt: TemplatedText | None = None) -> str:
    """Its config names ``jq/missing`` at node ``fetch``."""
    return text


def _jq_shape(kwargs: dict[str, Any]) -> list[TemplateRef]:
    raise ValueError("graph must be an object")


@tai42_app.tools.tool(template_refs=_jq_shape)
def jq_shape_tool(text: str = "") -> str:
    """Its template extractor refuses every blob as off-shape."""
    return text


def _jq_keyerror(kwargs: dict[str, Any]) -> list[TemplateRef]:
    raise KeyError("nodes")


@tai42_app.tools.tool(template_refs=_jq_keyerror)
def jq_keyerror_tool(text: str = "") -> str:
    """Its template extractor raises ``KeyError``."""
    return text


def _jq_str(kwargs: dict[str, Any]) -> list[Any]:
    return ["jq/missing"]


@tai42_app.tools.tool(template_refs=_jq_str)
def jq_str_tool(text: str = "") -> str:
    """Its template extractor answers a bare string entry."""
    return text
