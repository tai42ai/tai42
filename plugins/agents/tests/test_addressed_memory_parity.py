"""Each kind reports the memory its run kwargs address, so the platform's reservation sees every id.

The platform refuses a ``bridge:`` thread or checkpoint id among the ids a kind reports, so for
every config-shaped kwarg a kind reads the ids in its ``configurable`` (and a resume checkpoint)
must be reported; the continuity keys are the ones a door owning the run's identity refuses.
"""

from __future__ import annotations

import pytest
from tai42_contract.agent import AddressedMemory, Agent

from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.retrieval_tools_agent.agent import RetrievalToolsAgent
from tai42_agents.tools_agent import ToolsAgent
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.vqa_agent import VqaAgent

_RESERVED = "bridge:route:1.2.3.4"

# The config-shaped kwargs each kind reads.
_CONFIG_FIELDS: list[tuple[type[Agent], str]] = [
    (ToolsAgent, "langgraph_config"),
    (DeepAgent, "langgraph_config"),
    (RetrievalToolsAgent, "langgraph_config"),
    (VotingAgent, "judge_langgraph_config"),
    (VotingAgent, "voter_langgraph_config"),
    (RefineAgent, "evaluator_langgraph_config"),
    (RefineAgent, "critic_langgraph_config"),
]


@pytest.mark.parametrize(("kind", "field"), _CONFIG_FIELDS, ids=lambda v: getattr(v, "tool_name", v))
@pytest.mark.parametrize("key", ["thread_id", "checkpoint_id"])
def test_a_reserved_id_in_a_config_the_kind_reads_is_reported(kind: type[Agent], field: str, key: str) -> None:
    memory = kind.addressed_memory({field: {"configurable": {key: _RESERVED}}})
    reported = memory.thread_ids if key == "thread_id" else memory.checkpoint_ids
    assert reported == {_RESERVED}


@pytest.mark.parametrize("kind", [ToolsAgent, DeepAgent, RetrievalToolsAgent], ids=lambda k: k.tool_name)
def test_a_resume_checkpoint_is_reported(kind: type[Agent]) -> None:
    assert kind.addressed_memory({"resume_checkpoint_id": _RESERVED}).checkpoint_ids == {_RESERVED}


@pytest.mark.parametrize(("kind", "field"), _CONFIG_FIELDS, ids=lambda v: getattr(v, "tool_name", v))
def test_both_ids_of_a_config_are_reported(kind: type[Agent], field: str) -> None:
    run_kwargs = {field: {"configurable": {"thread_id": "user-42", "checkpoint_id": "cp-1"}}}
    memory = kind.addressed_memory(run_kwargs)
    assert memory.thread_ids == {"user-42"}
    assert memory.checkpoint_ids == {"cp-1"}


@pytest.mark.parametrize(
    ("kind", "keys"),
    [
        (ToolsAgent, {"resume", "resume_checkpoint_id", "checkpoint_provider"}),
        (DeepAgent, {"resume", "resume_checkpoint_id", "checkpoint_provider"}),
        (RetrievalToolsAgent, {"resume_checkpoint_id", "checkpoint_provider"}),
        (VotingAgent, {"checkpoint_provider"}),
        (RefineAgent, {"checkpoint_provider"}),
        (VqaAgent, set()),
        (ClaudeCodeAgent, set()),
    ],
    ids=lambda v: getattr(v, "tool_name", ""),
)
def test_each_kind_reports_the_continuity_keys_it_reads(kind: type[Agent], keys: set[str]) -> None:
    every = {"resume": "x", "resume_checkpoint_id": "cp", "checkpoint_provider": "redis", "user_message": "hi"}
    assert kind.addressed_memory(every).keys == keys


@pytest.mark.parametrize("kind", [VqaAgent, ClaudeCodeAgent], ids=lambda k: k.tool_name)
def test_a_kind_without_memory_reports_none(kind: type[Agent]) -> None:
    assert kind.addressed_memory({"langgraph_config": {"configurable": {"thread_id": _RESERVED}}}) == AddressedMemory()
