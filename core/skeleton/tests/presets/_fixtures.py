"""Base tools for the preset-engine tests.

Listed as the engine tests' ``tools`` module (``weather`` / ``echo`` / the
``tool_refs``-declaring ``plan_tool`` / ``boom_tool``): a preset bakes kwargs over
one of these, and its extension COMBOS attach the wrappers from
:mod:`tests.presets._ext_fixtures` as branches. The wrappers live in a separate
module so no single module is imported by two manifest sections.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.context import Context
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.presets import PresetInputSchemaSupport
from tai42_contract.secrets import SecretValue
from tai42_contract.tools import ComposedToolRef


@tai42_app.tools.tool
def weather(city: str, units: str = "metric") -> dict:
    """Report the weather for a city."""
    return {"city": city, "units": units}


@tai42_app.tools.tool
def secret_sink(token: SecretStr, label: str = "l", tokens: list[SecretStr] | None = None) -> str:
    """A base tool with secret-typed parameters (``SecretStr`` scalar and list).

    Exercises the preset read-view redaction: a literal baked into ``token`` /
    ``tokens`` is masked on a read, while the non-secret ``label`` is returned as
    authored."""
    return label


@tai42_app.tools.tool
def vault(account: str) -> dict:
    """Return an account's stored token wrapped as a secret."""
    return {"account": account, "token": SecretValue(f"tok-{account}")}


@tai42_app.tools.tool
def echo(text: str) -> str:
    """Echo the text back."""
    return text


@tai42_app.tools.tool
def failing_connector(query: str) -> CallToolResult:
    """A connector whose upstream call failed: an error result, flag set."""
    return CallToolResult(content=[TextContent(type="text", text=f"hub-err:upstream down for {query}")], isError=True)


@tai42_app.tools.tool
def payload_tool(payload: dict, tag: str = "t") -> dict:
    """A base tool with a structured ``payload`` argument.

    A preset over it can DECLARE input-schema support (the test registers
    ``PresetInputSchemaSupport(payload_arg="payload")``): the exposed tool advertises the
    authored ``input_schema`` and routes the caller's validated object into ``payload``."""
    return {"payload": payload, "tag": tag}


@tai42_app.tools.tool
async def ctx_echo(q: str, tone: str, ctx: Context) -> str:
    """Echo the query and tone, and whether a context was injected."""
    return f"{q}:{tone}:{ctx is not None}"


@tai42_app.tools.tool
async def payload_echo(payload: dict, tag: str) -> str:
    """Echo the payload's ``where`` and ``n`` and the tag."""
    return f"{payload['where']}:{payload.get('n')}:{tag}"


tai42_app.presets.register_input_schema_support("payload_echo", PresetInputSchemaSupport(payload_arg="payload"))


def _plan_refs(fixed_kwargs: dict[str, Any]) -> list[ComposedToolRef]:
    """The tools a ``plan_tool`` blob composes: ``plan.refs`` names (no site) and one entry
    per ``plan.steps`` item, sited ``step '<id>'`` and carrying the step's ``tool_kwargs``."""
    plan = fixed_kwargs.get("plan")
    if plan is None:
        return []
    if not isinstance(plan, dict):
        raise ValueError(f"plan must be an object; got {type(plan).__name__}")
    refs: list[ComposedToolRef] = []
    for entry in plan.get("refs", []):
        if not isinstance(entry, str):
            raise ValueError("plan.refs entries must be strings")
        refs.append(ComposedToolRef(name=entry))
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError(f"plan.steps must be a list; got {type(steps).__name__}")
    for step in steps:
        refs.append(
            ComposedToolRef(name=step["tool"], site=f"step '{step['id']}'", tool_kwargs=step.get("tool_kwargs"))
        )
    return refs


@tai42_app.tools.tool(tool_refs=_plan_refs, extras_keys=frozenset({"warm_start"}))
def plan_tool(text: str, plan: dict | None = None) -> str:
    """Compose over a plan that lists other tools by name under a nested key."""
    return text


@tai42_app.tools.tool(tool_refs=_plan_refs)
def plan_payload_tool(payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A ``plan_tool``-shaped extractor over a base whose presets may author an input."""
    return payload


tai42_app.presets.register_input_schema_support("plan_payload_tool", PresetInputSchemaSupport(payload_arg="payload"))


def _boom_refs(fixed_kwargs: dict[str, Any]) -> list[ComposedToolRef]:
    raise RuntimeError("refs extractor kaput")


@tai42_app.tools.tool(tool_refs=_boom_refs)
def boom_tool(text: str) -> str:
    """A base tool whose declared refs extractor always raises — the loud-error oracle."""
    return text
