"""The shared base of the test agents that address no memory of their own."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tai42_contract.agent import AddressedMemory, Agent


class MemorylessTestAgent(Agent):
    """A test agent whose run kwargs carry no thread or checkpoint: it reports ``AddressedMemory()``."""

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        return AddressedMemory()
