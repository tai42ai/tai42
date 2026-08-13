"""Base tools for the preset-engine tests.

Listed as the engine tests' ``tools`` module (``weather`` / ``echo`` / the
``tool_refs``-declaring ``plan_tool`` / ``boom_tool``): a preset bakes kwargs over
one of these, and its extension COMBOS attach the wrappers from
:mod:`tests.presets._ext_fixtures` as branches. The wrappers live in a separate
module so no single module is imported by two manifest sections.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def weather(city: str, units: str = "metric") -> dict:
    """Report the weather for a city."""
    return {"city": city, "units": units}


@tai42_app.tools.tool
def echo(text: str) -> str:
    """Echo the text back."""
    return text


def _plan_refs(fixed_kwargs: dict[str, Any]) -> list[str]:
    """Read the composed tool names out of a ``plan_tool`` preset's nested ``plan``
    config — the extractor a base tool declares so the platform never learns its
    config shape. Entries stay verbatim; a non-string entry is a caller bug the
    reference collector raises on."""
    return fixed_kwargs.get("plan", {}).get("refs", [])


@tai42_app.tools.tool(tool_refs=_plan_refs)
def plan_tool(text: str, plan: dict | None = None) -> str:
    """Compose over a plan that lists other tools by name under a nested key."""
    return text


def _boom_refs(fixed_kwargs: dict[str, Any]) -> list[str]:
    raise RuntimeError("refs extractor kaput")


@tai42_app.tools.tool(tool_refs=_boom_refs)
def boom_tool(text: str) -> str:
    """A base tool whose declared refs extractor always raises — the loud-error oracle."""
    return text
