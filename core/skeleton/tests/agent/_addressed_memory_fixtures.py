"""A neutral synthetic agent whose memory address rides nested inside its own option blob.

``ScopedMemoryAgent`` names no engine: its ``engine_options`` input carries a thread id at
``engine_options.scope.key`` and a checkpoint id at ``engine_options.scope.mark``, and its
``addressed_memory`` reports both — the only way the platform can see them. ``engine_options.resume_from``
is a key that steers the run's continuity, reported in ``keys``. Every mapping handed to
``addressed_memory`` is recorded, so a test observes what each door asked about.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from tai42_contract.agent import AddressedMemory, Agent
from tai42_contract.app import tai42_app

addressed_memory_seen: list[dict[str, Any]] = []
run_seen: list[dict[str, Any]] = []


class ScopedMemoryInput(BaseModel):
    text: str
    engine_options: dict[str, Any] | None = None
    resume_from: str | None = None


@tai42_app.agents.agent("scoped_memory")
class ScopedMemoryAgent(Agent):
    tool_name = "scoped_memory"
    tool_description = "An agent addressing its memory through its own option blob."
    ToolInput = ScopedMemoryInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        addressed_memory_seen.append(dict(run_kwargs))
        options = run_kwargs.get("engine_options")
        scope = options.get("scope") if isinstance(options, Mapping) else None
        thread_ids: set[str] = set()
        checkpoint_ids: set[str] = set()
        if isinstance(scope, Mapping):
            if isinstance(scope.get("key"), str):
                thread_ids.add(scope["key"])
            if isinstance(scope.get("mark"), str):
                checkpoint_ids.add(scope["mark"])
        keys = frozenset({"resume_from"} & set(run_kwargs))
        return AddressedMemory(keys=keys, thread_ids=frozenset(thread_ids), checkpoint_ids=frozenset(checkpoint_ids))

    async def run(self, **kwargs: Any) -> str:
        run_seen.append(dict(kwargs))
        options = kwargs.get("engine_options") or {}
        scope = options.get("scope") or {}
        return str(kwargs.get("thread_id") or scope.get("key") or "")
