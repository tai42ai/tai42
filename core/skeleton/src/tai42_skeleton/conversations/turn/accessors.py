"""Lazy accessors to the registries and tool runner a turn uses.

Each reaches the running app for its agent registry or tool runner at call time — the single
seam turn code goes through so the app is resolved once, at call time. The conversations
stores are served by the conversations manager.
"""

from __future__ import annotations

from tai42_contract.agent import Agent


def _agent_registry() -> dict[str, Agent]:
    from tai42_skeleton.app import instance

    return instance.app.agents.all_agents()


def _tools():
    from tai42_skeleton.app import instance

    return instance.app.tools
