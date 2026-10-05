"""Real-provider routing: a kit-built ``ChatOpenAI`` takes the native plan for a native id.

This covers the e2e stack reality without the stack: the structured-output plan is decided
against the model the kit's ``get_llm`` actually builds and the capability profile the provider
package attaches to it. A native-capable id (``gpt-4o-mini`` declares ``structured_output``)
routes to the provider-native plan (no forced tool choice); a profile-less id (an unknown
sentinel) legitimately falls to the tool tier. The e2e mock leg must therefore present a
native-capable model id for its structured runs to exercise the native path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_openai")

from tai42_kit.llm.models import get_llm
from tai42_kit.llm.structured import plan_structured_output

from tai42_agents._internal.structured import NativeStrategy, structured_output_stack

_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}


def test_gpt_4o_mini_declares_native_and_routes_native() -> None:
    llm = get_llm("openai", model="gpt-4o-mini", api_key="x")
    assert (llm.profile or {}).get("structured_output") is True
    plan = plan_structured_output(llm, "openai", _SCHEMA)
    assert plan.mode == "native"
    # The minted strategy is the native one and binds the response_format kwarg (no tool_choice).
    strategy, rail = structured_output_stack(llm, "openai", _SCHEMA)
    assert isinstance(strategy, NativeStrategy)
    assert "response_format" in strategy.to_model_kwargs()
    assert rail is not None


def test_profile_less_model_id_falls_to_the_tool_tier() -> None:
    # An id the provider package does not know carries no profile, so native is not declared
    # and the plan takes the bounded tool tier — the e2e ``e2e-scripted`` sentinel's behaviour,
    # which is why the mock leg must present a native-capable id for its structured runs.
    llm = get_llm("openai", model="not-a-real-model-xyz", api_key="x")
    assert (llm.profile or {}).get("structured_output") is not True
    plan = plan_structured_output(llm, "openai", _SCHEMA)
    assert plan.mode == "tool"
