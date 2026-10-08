"""``_reset_component_surface`` clears every stored component off the app's local provider.

The surface holds one tool (disabled at the server, so the server ``list_*`` view hides it),
one prompt, one resource and one resource template; after the reset the provider stores
none of them. The reset is synchronous and runs both from a loop-less thread (a reload's
worker thread) and from inside a running loop (cold boot on the serving loop).
"""

from __future__ import annotations

from tai42_skeleton.app.server import TaiMCP


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


def _populated() -> TaiMCP:
    instance = TaiMCP(name="reset-surface-under-test")
    mcp = instance._fast_mcp
    mcp.tool(_tool_a, name="tool_a")
    mcp.prompt(_prompt_a, name="prompt_a")
    mcp.resource("probe://static")(_resource_a)
    mcp.resource("probe://item/{item_id}")(_template_a)
    mcp.disable(names={"tool_a"})
    return instance


async def _stored(instance: TaiMCP) -> list[object]:
    provider = instance._fast_mcp.local_provider
    return [
        *(await provider.list_tools()),
        *(await provider.list_prompts()),
        *(await provider.list_resources()),
        *(await provider.list_resource_templates()),
    ]


def test_the_reset_from_a_loop_less_thread_empties_the_surface() -> None:
    import asyncio

    instance = _populated()
    assert len(asyncio.run(_stored(instance))) == 4
    instance._reset_component_surface()
    assert asyncio.run(_stored(instance)) == []


async def test_the_reset_inside_a_running_loop_empties_the_surface() -> None:
    instance = _populated()
    assert len(await _stored(instance)) == 4
    instance._reset_component_surface()
    assert await _stored(instance) == []
