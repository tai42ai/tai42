"""Each agent kind declares whether a run of it can park (``Agent.parks``)."""

from __future__ import annotations

import pytest

from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.retrieval_tools_agent.agent import RetrievalToolsAgent
from tai42_agents.tools_agent import ToolsAgent
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.vqa_agent import VqaAgent


@pytest.mark.parametrize(
    ("kind", "parks"),
    [
        (ToolsAgent, True),
        (DeepAgent, True),
        (ClaudeCodeAgent, True),
        (VqaAgent, False),
        (VotingAgent, False),
        (RefineAgent, False),
        (RetrievalToolsAgent, False),
    ],
)
def test_each_kind_declares_whether_it_parks(kind: type, parks: bool) -> None:
    assert kind.parks is parks
