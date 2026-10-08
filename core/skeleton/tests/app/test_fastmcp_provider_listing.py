"""Vendor assumption: FastMCP's local provider lists EVERY stored component through its public API.

``_reset_component_surface`` enumerates the local provider's raw stored components through the
provider's public ``list_tools`` / ``list_prompts`` / ``list_resources`` /
``list_resource_templates``. Those return every stored component, disabled ones included (the
server view filters; the provider view only marks). This test turns red on a FastMCP that
changes it.
"""

from __future__ import annotations

from fastmcp import FastMCP


def _tool_a() -> str:
    """A tool."""
    return "a"


def _prompt_a() -> str:
    """A prompt."""
    return "p"


def _resource_a() -> str:
    """A resource."""
    return "r"


def _template_a(item_id: str) -> str:
    """A resource template."""
    return item_id


async def test_the_provider_listings_return_every_stored_component_disabled_ones_included() -> None:
    mcp = FastMCP(name="provider-listing-probe")
    mcp.tool(_tool_a, name="tool_a")
    mcp.prompt(_prompt_a, name="prompt_a")
    mcp.resource("probe://static")(_resource_a)
    mcp.resource("probe://item/{item_id}")(_template_a)
    mcp.disable(names={"tool_a"})

    # The server view filters the disabled tool out.
    assert "tool_a" not in {tool.name for tool in await mcp.list_tools()}

    provider = mcp.local_provider
    assert [tool.name for tool in await provider.list_tools()] == ["tool_a"]
    assert [prompt.name for prompt in await provider.list_prompts()] == ["prompt_a"]
    assert [str(resource.uri) for resource in await provider.list_resources()] == ["probe://static"]
    assert [template.uri_template for template in await provider.list_resource_templates()] == [
        "probe://item/{item_id}"
    ]
