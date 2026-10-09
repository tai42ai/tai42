"""Which of the agents' tools can pause (``tai42/pauses``).

A kind's run tool is declared through its ``parks`` class attribute, never its registration ``meta``; the
park continuations (``agent_resume``, the chained-park delivery) declare it at registration.
"""

from __future__ import annotations

import pytest
from tai42_contract.tools import TOOL_META_PAUSES

from tai42_agents import tools_agent as _tools_agent  # noqa: F401
from tai42_agents import vqa_agent as _vqa_agent  # noqa: F401
from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME, register_chained_park_tool
from tai42_agents._internal.park.resume_tool import AGENT_RESUME_TOOL_NAME, register_agent_resume_tool
from tai42_agents.claude_code import agent as _claude_agent  # noqa: F401
from tai42_agents.langchain_deep_agent import agent as _deep_agent  # noqa: F401
from tai42_agents.refine_agent import agent as _refine_agent  # noqa: F401
from tai42_agents.retrieval_tools_agent import agent as _retrieval_agent  # noqa: F401
from tai42_agents.voting_agent import agent as _voting_agent  # noqa: F401

from .conftest import APP


@pytest.mark.parametrize(
    "name",
    [
        "tools_agent",
        "langchain_deep_agent",
        "claude_code",
        "refine_agent",
        "voting_agent",
        "vqa_agent",
        "retrieval_tools_agent",
    ],
)
def test_no_agent_registration_declares_the_pause_meta_itself(name: str) -> None:
    # A kind's run-tool pause declaration comes from its ``parks`` class attribute (the platform
    # derives the meta from it); a registration naming the meta key itself is refused.
    assert TOOL_META_PAUSES not in APP.agents.meta[name]


@pytest.mark.parametrize(
    ("register", "name"),
    [
        (register_agent_resume_tool, AGENT_RESUME_TOOL_NAME),
        (register_chained_park_tool, CHAINED_PARK_DELIVERY_TOOL_NAME),
    ],
)
def test_a_park_continuation_declares_it_can_pause(register, name: str) -> None:
    register()
    assert APP.tools.registered_meta[name].get(TOOL_META_PAUSES) is True
