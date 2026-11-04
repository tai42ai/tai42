"""A release closes the LangGraph checkpoint and store registries the kit keeps for the running loop."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm import checkpoint_registry, store_registry
from tai42_kit.loop_bound import release_loop_bound_resources


async def test_the_checkpoint_and_store_registries_are_released():
    checkpoints = checkpoint_registry()
    stores = store_registry()
    await checkpoints.get_checkpointer("memory", None)
    await stores.get_store("memory", None)
    assert (checkpoints.has_live_resources, stores.has_live_resources) == (True, True)

    await release_loop_bound_resources()

    assert (checkpoints.has_live_resources, stores.has_live_resources) == (False, False)
    rebuilt_checkpoints = checkpoint_registry()
    rebuilt_stores = store_registry()
    await rebuilt_checkpoints.get_checkpointer("memory", None)
    await rebuilt_stores.get_store("memory", None)
    assert (rebuilt_checkpoints.has_live_resources, rebuilt_stores.has_live_resources) == (True, True)

    await release_loop_bound_resources()

    assert (rebuilt_checkpoints.has_live_resources, rebuilt_stores.has_live_resources) == (False, False)
