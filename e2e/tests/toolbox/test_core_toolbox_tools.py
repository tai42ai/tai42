"""The five toolbox tools on the core stack.

The core profile carries ``request`` / ``generate_embeddings`` / ``pad_embeddings`` /
``current_time_info`` / ``classify``; each is driven over the server's own MCP surface:

* ``request`` against the harness recording target server (``recording_proxy.TargetServer``),
  reached over loopback (the core stack opts the loopback CIDRs into the SSRF guard).
* ``generate_embeddings`` against the LLM stub's ``/v1/embeddings`` via the tool's per-call
  ``base_url`` (``embedding_kwargs``), so no real embedding provider is contacted — the
  real-provider boundary is a separate leg.
* ``pad_embeddings`` widening the returned vectors, and ``current_time_info`` returning its
  structured clock — both pure, no backing service.
* ``classify`` against the LLM stub's deterministic ``/v1/systemone`` via the tool's per-call
  ``base_url`` (``classifier_kwargs``, the stub's ROOT url — the vendor appends
  ``/v1/systemone`` itself), so no real classifier provider is contacted — the real-provider
  boundary is a separate leg.
"""

from __future__ import annotations

import math

import pytest
from tai42_kit.llm.classifier import ChoiceAnswer, ClassifyResponse, NoulAnswer, ScoreAnswer

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.recording_proxy import TargetServer
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack


@pytest.mark.needs(
    "setting:tool:toolbox.request",
    "helper:target-server",
    "setting:TAI_URL_GUARD_ALLOW_CIDRS=loopback",
)
async def test_request_tool_hits_the_target_server(core_stack: TaiStack, target_server: TargetServer) -> None:
    before = len(target_server.records)
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        result = await mcp.call_tool("request", {"url": f"{target_server.url}/ok"}, retry_on_reloading=True)
    data = result.data
    assert data["status_code"] == 200, data
    assert data["text"] == "target-ok", data
    # The SUT actually dialled the target (recorded in the same process as this test).
    hits = [r for r in target_server.records[before:] if r.path == "/ok"]
    assert len(hits) == 1, target_server.records[before:]


# The stub's deterministic /v1/embeddings (identical input → identical vectors) IS the
# embeddings MOCK leg, so this test steps aside when the 'embeddings' seam is real — the
# real leg replaces the stub on the creds host.
@pytest.mark.skipif(
    HarnessSettings().is_real("embeddings"),
    reason="stub-embeddings determinism is the 'embeddings' mock leg; the real leg on the creds host",
)
@pytest.mark.needs("setting:tool:toolbox.generate_embeddings", "setting:tool:toolbox.pad_embeddings", "helper:llm")
async def test_generate_and_pad_embeddings(core_stack: TaiStack, llm_stub: LlmStub) -> None:
    call_args = {
        "texts": ["alpha", "beta"],
        # The tool's per-call base_url override → the LLM stub's deterministic /v1/embeddings.
        "embedding_kwargs": {"base_url": llm_stub.base_url, "api_key": "e2e-test"},
    }
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        first = (await mcp.call_tool("generate_embeddings", call_args, retry_on_reloading=True)).data
        second = (await mcp.call_tool("generate_embeddings", call_args)).data

    assert isinstance(first, list), first
    assert len(first) == 2, first
    width = len(first[0])
    assert width > 0, first
    assert all(len(vec) == width for vec in first), first
    # The stub is deterministic: identical input yields identical vectors.
    assert first == second, (first, second)

    target_dim = width + 8
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        padded = (await mcp.call_tool("pad_embeddings", {"embeddings": first, "target_dim": target_dim})).data
    assert len(padded) == 2, padded
    assert all(len(vec) == target_dim for vec in padded), padded
    # The leading components are preserved; the tail is zero-padded.
    assert padded[0][:width] == first[0], (padded[0], first[0])
    assert padded[0][width:] == [0.0] * 8, padded[0]


@pytest.mark.needs("setting:tool:toolbox.current_time_info")
async def test_current_time_info(core_stack: TaiStack) -> None:
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        info = (await mcp.call_tool("current_time_info", retry_on_reloading=True)).data
    assert set(info) >= {"utc", "local", "system"}, info
    assert info["utc"]["year"] >= 2024, info["utc"]
    assert isinstance(info["system"]["epoch_seconds"], (int, float)), info["system"]


# One question of each kind against a small generic state object; the stub's /v1/systemone is
# deterministic (each answer fixed by its ``type``), so identical input yields identical answers.
_CLASSIFY_STATE = {"kind": "status-update", "level": "info", "count": 3, "active": True}
_CLASSIFY_QUESTIONS = {
    "is_active": {"type": "noul", "instructions": "Is the event currently active?"},
    "status": {
        "type": "choice",
        "instructions": "Which status best fits the state?",
        "criteria": {"open": "still ongoing", "closed": "already finished"},
    },
    "severity": {
        "type": "score",
        "instructions": "Rate the severity of the state.",
        "criteria": ["low", "medium", "high"],
    },
}


def _assert_classify_shape(response: ClassifyResponse) -> None:
    """The response is a kit ``ClassifyResponse``: one answer per requested question, keyed
    by the question name, each carrying the ``type`` its question kind maps to, plus usage
    and a request id."""
    assert set(response.answers) == set(_CLASSIFY_QUESTIONS), response.answers
    kinds = {name: question["type"] for name, question in _CLASSIFY_QUESTIONS.items()}
    for name, answer in response.answers.items():
        assert answer.type == kinds[name], (name, answer)
    assert response.usage is not None, response
    assert response.request_id is not None, response


def _assert_classify_values(response: ClassifyResponse) -> None:
    """The stub hashes each answer from the state and question, so the values are the
    stub's to assert (unlike the real leg's, which are the vendor's): a noul in
    ``[0, 1]``; a choice whose label and probability keys are the question's criteria;
    a score within the ordinal range whose probability keys are the level indices — and
    every distribution summing to 1."""
    for name, question in _CLASSIFY_QUESTIONS.items():
        answer = response.answers[name]
        if isinstance(answer, NoulAnswer):
            assert 0.0 <= answer.noul <= 1.0, answer
        elif isinstance(answer, ChoiceAnswer):
            labels = set(question["criteria"])
            assert answer.choice in labels, answer
            assert set(answer.probabilities) == labels, answer
            assert answer.probabilities[answer.choice] == max(answer.probabilities.values()), answer
            assert math.isclose(sum(answer.probabilities.values()), 1.0, rel_tol=1e-9), answer
        elif isinstance(answer, ScoreAnswer):
            levels = question["criteria"]
            assert 0.0 <= answer.score <= len(levels) - 1, answer
            assert set(answer.probabilities) == set(range(len(levels))), answer
            assert math.isclose(sum(answer.probabilities.values()), 1.0, rel_tol=1e-9), answer


# ``classify`` against the stub's deterministic /v1/systemone IS the classifier MOCK leg, so
# this test steps aside when the 'classifier' seam is real — the real leg replaces the stub.
@pytest.mark.skipif(
    HarnessSettings().is_real("classifier"),
    reason="stub-classify determinism is the 'classifier' mock leg; the real leg on the creds host",
)
@pytest.mark.needs("setting:tool:toolbox.classify", "helper:llm")
async def test_classify_is_deterministic_against_the_stub(core_stack: TaiStack, llm_stub: LlmStub) -> None:
    call_args = {
        "state": _CLASSIFY_STATE,
        "questions": _CLASSIFY_QUESTIONS,
        # The stub exposes its ROOT origin; the vendor appends ``/v1/systemone`` itself (unlike
        # the embeddings leg, whose ``base_url`` already carries ``/v1``).
        "classifier_kwargs": {"base_url": llm_stub.root_url, "api_key": "e2e-test"},
    }
    # A different state must move at least one answer: the stub hashes the state into every
    # answer, so a state dropped or corrupted on the wire is detectable here.
    other_args = {**call_args, "state": {**_CLASSIFY_STATE, "count": _CLASSIFY_STATE["count"] + 1}}
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        first = await mcp.call_tool("classify", call_args, retry_on_reloading=True)
        second = await mcp.call_tool("classify", call_args)
        other = await mcp.call_tool("classify", other_args)

    first_response = ClassifyResponse.model_validate(first.structured_content)
    second_response = ClassifyResponse.model_validate(second.structured_content)
    other_response = ClassifyResponse.model_validate(other.structured_content)
    _assert_classify_shape(first_response)
    _assert_classify_values(first_response)
    # The stub is deterministic: identical input yields the identical answers.
    assert first_response.answers == second_response.answers, (first_response, second_response)
    # A different state yields a different answer set.
    assert other_response.answers != first_response.answers, (other_response, first_response)


# The real classifier leg runs the tool against the live vendor instead of the stub, so it
# runs only when the 'classifier' seam is real (creds host); the provider and key flow from
# the stack env, so the call passes NO classifier_kwargs.
@pytest.mark.skipif(
    not HarnessSettings().is_real("classifier"),
    reason="the real classify leg needs TYPESAFE_API_KEY (TAI_E2E_REAL=classifier); creds host only",
)
@pytest.mark.needs("setting:tool:toolbox.classify", "setting:real-credentials")
async def test_classify_against_the_real_vendor(core_stack: TaiStack) -> None:
    call_args = {"state": _CLASSIFY_STATE, "questions": _CLASSIFY_QUESTIONS}
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        result = await mcp.call_tool("classify", call_args, retry_on_reloading=True)
    # The vendor's exact probabilities are the model's; the real leg asserts the response
    # shape only, never the values.
    _assert_classify_shape(ClassifyResponse.model_validate(result.structured_content))
