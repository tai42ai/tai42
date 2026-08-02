"""Non-validator real-logic tests — the concrete behavior the contract carries
beyond field/model validation: the ``Agent`` terminal-rule drain + default
stream, the ``ConnectionRecord`` health/serialization helpers, the
``TaiMCPConfig`` transport properties, the storage-root guard, and the
behavioral error/enum members.

ABC abstract-method bodies (``raise NotImplementedError`` / ``...`` stubs) are
contract scaffolding, not logic, and are intentionally not exercised.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import pydantic
import pytest

from tai42_contract.agent.base import (
    Agent,
    AgentInterruptedError,
    PresetSpec,
    SubAgentSpec,
)
from tai42_contract.agent.events import (
    InterruptFinal,
    MessageFinal,
    ReasoningStep,
    StreamEvent,
    StructuredFinal,
)
from tai42_contract.connectors.errors import OperatorMisconfiguredError
from tai42_contract.connectors.models import AuthHealthState, ConnectionRecord, ConnectorRef
from tai42_contract.extensions import ExtensionKind
from tai42_contract.extensions.kinds import ExtensionFactory
from tai42_contract.manifest import Manifest, MCPConfig, TaiMCPConfig
from tai42_contract.storage import assert_not_root

UUID = "12345678-1234-1234-1234-123456789abc"


# === agent/base.py ==========================================================


class _DummyAgent(Agent):
    """Minimal concrete Agent: ``run`` returns whatever it is told to, so the
    default ``astream`` / inherited ``_drain`` can be exercised directly."""

    tool_name = "dummy"
    ToolInput = PresetSpec

    def __init__(self, return_value: Any = "ran") -> None:
        self._return_value = return_value

    async def run(self, **kwargs: Any) -> Any:
        return self._return_value


async def _agen(events: Sequence[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for event in events:
        yield event


# -- AgentInterruptedError.__init__ -----------------------------------------------


def test_agent_interrupted_carries_interrupts_and_ids():
    interrupts = [InterruptFinal(interrupt_id="a"), InterruptFinal(interrupt_id="b")]
    exc = AgentInterruptedError(interrupts)
    assert exc.interrupts == interrupts
    assert "a, b" in str(exc)


def test_agent_interrupted_empty_ids_says_unknown():
    exc = AgentInterruptedError([])
    assert exc.interrupts == []
    assert "unknown" in str(exc)


# -- Agent.from_tool_input (set-fields-only mapping) -------------------------


def test_from_tool_input_maps_only_set_fields():
    validated = PresetSpec(name="x", base_tool="flow")
    out = Agent.from_tool_input(validated)
    # description / fixed_kwargs were left at their defaults -> not in fields_set.
    assert out == {"name": "x", "base_tool": "flow"}


def test_from_tool_input_keeps_nested_models_as_instances():
    preset = PresetSpec(name="p", base_tool="flow")
    spec = SubAgentSpec(name="agent", presets=[preset])
    out = Agent.from_tool_input(spec)
    assert set(out) == {"name", "presets"}
    assert out["presets"] == [preset]
    assert isinstance(out["presets"][0], PresetSpec)


def test_sub_agent_spec_response_format_accepts_a_json_schema_dict():
    # ``response_format`` accepts a JSON-Schema dict on the neutral spec (not only a
    # pydantic class), the dict shape the deep sub-agent path already authors.
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
    spec = SubAgentSpec(name="agent", response_format=schema)
    assert spec.response_format == schema


def test_sub_agent_spec_response_format_accepts_a_pydantic_class():
    class Answer(pydantic.BaseModel):
        value: int

    spec = SubAgentSpec(name="agent", response_format=Answer)
    assert spec.response_format is Answer


# -- spec_runnable capability marker (ClassVar, not an instance field) --------


def test_spec_runnable_defaults_false_on_base_agent():
    # A code role-agent is NOT authorable unless it opts in.
    assert Agent.spec_runnable is False


def test_spec_runnable_subclass_can_declare_true():
    class _Authorable(_DummyAgent):
        spec_runnable = True

    assert _Authorable.spec_runnable is True
    # The base default is untouched by the subclass override.
    assert Agent.spec_runnable is False


def test_spec_runnable_is_a_classvar_not_an_instance_field():
    # It is a class attribute (readable off the class, no instance needed) and
    # never leaks onto instance state.
    assert "spec_runnable" in Agent.__annotations__
    agent = _DummyAgent()
    assert agent.spec_runnable is False
    assert "spec_runnable" not in vars(agent)


# -- preset_bakeable_fields declaration (ClassVar, not an instance field) -----


def test_preset_bakeable_fields_defaults_empty_on_base_agent():
    # Nothing is preset-bakeable on a non-spec_runnable agent unless it opts in.
    assert Agent.preset_bakeable_fields == frozenset()


def test_preset_bakeable_fields_subclass_can_declare_non_empty():
    class _Locked(_DummyAgent):
        preset_bakeable_fields = frozenset({"base_tool", "fixed_kwargs"})

    assert _Locked.preset_bakeable_fields == frozenset({"base_tool", "fixed_kwargs"})
    # The base default is untouched by the subclass override.
    assert Agent.preset_bakeable_fields == frozenset()


def test_preset_bakeable_fields_is_a_classvar_not_an_instance_field():
    # It is a class attribute (readable off the class, no instance needed) and
    # never leaks onto instance state.
    assert "preset_bakeable_fields" in Agent.__annotations__
    agent = _DummyAgent()
    assert agent.preset_bakeable_fields == frozenset()
    assert "preset_bakeable_fields" not in vars(agent)


# -- default astream (one type-correct terminal) -----------------------------


def _astream_events(agent: Agent) -> list[StreamEvent]:
    async def collect() -> list[StreamEvent]:
        return [event async for event in agent.astream(user_message="hi")]

    return asyncio.run(collect())


def test_default_astream_emits_structured_terminal_for_non_str():
    agent = _DummyAgent(return_value={"k": "v"})
    events = _astream_events(agent)
    assert len(events) == 1
    assert isinstance(events[0], StructuredFinal)
    assert events[0].data == {"k": "v"}
    assert events[0].final is True


def test_default_astream_emits_message_terminal_for_str():
    # A plain-text answer is a MessageFinal, not a misclassified StructuredFinal.
    agent = _DummyAgent(return_value="plain text")
    events = _astream_events(agent)
    assert len(events) == 1
    assert isinstance(events[0], MessageFinal)
    assert events[0].text == "plain text"
    assert events[0].final is True


# -- _drain — every terminal-rule branch -------------------------------------


def test_drain_returns_structured_data():
    agent = _DummyAgent()
    result = asyncio.run(agent._drain(_agen([StructuredFinal(data={"out": 1})])))  # pyright: ignore[reportPrivateUsage]
    assert result == {"out": 1}


def test_drain_falls_back_to_message_text():
    agent = _DummyAgent()
    result = asyncio.run(agent._drain(_agen([MessageFinal(text="hello")])))  # pyright: ignore[reportPrivateUsage]
    assert result == "hello"


def test_drain_structured_wins_over_message():
    agent = _DummyAgent()
    events = [MessageFinal(text="hello"), StructuredFinal(data="structured")]
    assert asyncio.run(agent._drain(_agen(events))) == "structured"  # pyright: ignore[reportPrivateUsage]


def test_drain_no_terminal_returns_empty_string():
    agent = _DummyAgent()
    # A non-terminal event only -> no structured/message terminal seen.
    result = asyncio.run(agent._drain(_agen([ReasoningStep(text="thinking")])))  # pyright: ignore[reportPrivateUsage]
    assert result == ""


def test_drain_interrupt_raises_agent_interrupted():
    agent = _DummyAgent()
    events = [InterruptFinal(interrupt_id="x"), StructuredFinal(data="ignored")]
    with pytest.raises(AgentInterruptedError) as info:
        asyncio.run(agent._drain(_agen(events)))  # pyright: ignore[reportPrivateUsage]
    assert [i.interrupt_id for i in info.value.interrupts] == ["x"]


def test_drain_response_format_unmet_raises_loud():
    agent = _DummyAgent()
    with pytest.raises(RuntimeError, match=r"produced no\s+structured output"):
        asyncio.run(agent._drain(_agen([MessageFinal(text="text only")]), response_format=dict))  # pyright: ignore[reportPrivateUsage]


def test_drain_response_format_met_returns_structured():
    agent = _DummyAgent()
    result = asyncio.run(agent._drain(_agen([StructuredFinal(data={"ok": True})]), response_format=dict))  # pyright: ignore[reportPrivateUsage]
    assert result == {"ok": True}


# === connectors/models.py — ConnectionRecord helpers ========================


def _oauth_record(**overrides: Any) -> ConnectionRecord:
    base: dict[str, Any] = {
        "connection_id": UUID,
        "provider_id": "google",
        "alias": "my-google",
        "kind": "oauth",
        "account_identity": "me@example.com",
        "enabled_sub_services": ["gmail"],
        "access_token": "a-token",
        "refresh_token": "r-token",
        "access_token_expires_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ConnectionRecord(**base)


def _noauth_record(**overrides: Any) -> ConnectionRecord:
    base: dict[str, Any] = {
        "connection_id": UUID,
        "provider_id": "local",
        "alias": "files",
        "kind": "none",
        "enabled_sub_services": ["files"],
        "config_values": {"api_key": "secret-v"},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ConnectionRecord(**base)


def test_is_healthy_true_only_for_healthy_state():
    assert _oauth_record(auth_health_state=AuthHealthState.HEALTHY).is_healthy() is True
    assert _oauth_record(auth_health_state=AuthHealthState.RECONNECT_REQUIRED).is_healthy() is False
    assert _oauth_record(auth_health_state=AuthHealthState.REFRESH_FAILING).is_healthy() is False


def test_to_storage_json_exposes_oauth_token_plaintext():
    rec = _oauth_record()
    data = json.loads(rec.to_storage_json())
    assert data["access_token"] == "a-token"
    assert data["refresh_token"] == "r-token"
    # oauth carries no config_values.
    assert data["config_values"] == {}
    # the in-memory repr keeps the SecretStr redaction.
    assert "a-token" not in repr(rec)


def test_to_storage_json_exposes_config_values_and_null_tokens():
    rec = _noauth_record()
    data = json.loads(rec.to_storage_json())
    assert data["access_token"] is None
    assert data["refresh_token"] is None
    assert data["config_values"] == {"api_key": "secret-v"}
    assert "secret-v" not in repr(rec)


# === manifest — TaiMCPConfig.is_managed =====================================


def _ref() -> ConnectorRef:
    return ConnectorRef(connection_id=UUID, provider_id="google", sub_service="gmail")


def test_taimcpconfig_is_managed_true_and_false():
    unmanaged = TaiMCPConfig(title="t", config=MCPConfig(url="https://x"))
    assert unmanaged.is_managed is False
    managed = TaiMCPConfig(title="t", config=MCPConfig(url="https://x"), managed=_ref())
    assert managed.is_managed is True


# === manifest — default_routers field =======================================


def test_default_routers_defaults_to_all():
    assert Manifest().default_routers == "all"


@pytest.mark.parametrize("value", ["all", "api", "none"])
def test_default_routers_accepts_the_three_literals(value: Literal["all", "api", "none"]):
    assert Manifest(default_routers=value).default_routers == value


@pytest.mark.parametrize("bad", ["studio", "", "ALL", 1])
def test_default_routers_rejects_anything_else(bad: Any):
    with pytest.raises(pydantic.ValidationError, match="default_routers"):
        Manifest(default_routers=bad)


def test_default_routers_survives_model_dump_round_trip():
    # A NORMAL serialized field (no exclude): it must appear in ``model_dump``
    # (so it reaches ``live_manifest``) and re-validate unchanged.
    dumped = Manifest(default_routers="none").model_dump()
    assert dumped["default_routers"] == "none"
    assert Manifest.model_validate(dumped).default_routers == "none"


def test_default_routers_is_orthogonal_to_routers_modules():
    # The opt-out coexists with an explicit extras list — they are independent:
    # ``"none"`` with a populated ``routers_modules`` is a legal manual surface.
    manifest = Manifest(default_routers="none", routers_modules=["tai42_plugin.routers.door"])
    assert manifest.default_routers == "none"
    assert manifest.routers_modules == ["tai42_plugin.routers.door"]


# === manifest — behavioral stubs raise on the bare contract model ===========


def test_manifest_behavioral_stubs_raise_not_implemented():
    # The contract ``Manifest`` carries only the signatures; the impl subclass
    # supplies the bodies. Calling one on the bare contract model must raise
    # loudly, never silently return None.
    manifest = Manifest()
    with pytest.raises(NotImplementedError, match="should_include_tool"):
        manifest.should_include_tool("name", "module")
    with pytest.raises(NotImplementedError, match="should_include_agent"):
        manifest.should_include_agent("name", "module")
    with pytest.raises(NotImplementedError, match="should_include_mcp_tool"):
        manifest.should_include_mcp_tool("name", "title")
    with pytest.raises(NotImplementedError, match="replace_mcp"):
        manifest.replace_mcp([])
    with pytest.raises(NotImplementedError, match="live_manifest"):
        _ = manifest.live_manifest
    with pytest.raises(NotImplementedError, match="find_title"):
        manifest.find_title("module")


# === storage.assert_not_root ================================================


def test_assert_not_root_accepts_real_path():
    # Returns None (no raise) for a concrete directory path.
    assert assert_not_root("some/dir") is None


@pytest.mark.parametrize("bad", ["", "   ", "/", ".", "..", "/./"])
def test_assert_not_root_rejects_root_like_paths(bad: str):
    with pytest.raises(ValueError, match="Refusing to delete the storage root"):
        assert_not_root(bad)


@pytest.mark.parametrize("bad", ["a/..", "./x/../..", "../etc"])
def test_assert_not_root_rejects_paths_normalizing_to_or_above_root(bad: str):
    # Paths whose normalized form escapes to (or above) the root must be refused.
    with pytest.raises(ValueError, match="Refusing to delete the storage root"):
        assert_not_root(bad)


@pytest.mark.parametrize("ok", ["a/b", "a/../b"])
def test_assert_not_root_accepts_paths_normalizing_inside_root(ok: str):
    # Internal ".." segments that still resolve to a concrete sub-path are fine.
    assert assert_not_root(ok) is None


# === connectors/errors.py — OperatorMisconfiguredError ======================


def test_operator_misconfigured_error_carries_fields_and_message():
    exc = OperatorMisconfiguredError(env_var="GOOGLE_CLIENT_ID", provider_id="google")
    assert exc.env_var == "GOOGLE_CLIENT_ID"
    assert exc.provider_id == "google"
    assert "GOOGLE_CLIENT_ID" in str(exc)
    assert "google" in str(exc)
    assert isinstance(exc, RuntimeError)


# === extensions — ExtensionKind.multiple ====================================


def test_extension_kind_multiple_cardinality():
    assert ExtensionKind.WRAPPER.multiple is True
    assert ExtensionKind.TRANSFORMER.multiple is True
    assert ExtensionKind.BACKEND.multiple is False


def test_extension_kind_schema_properties():
    # preserves_schema: only the schema-transparent WRAPPER.
    assert ExtensionKind.WRAPPER.preserves_schema is True
    assert ExtensionKind.TRANSFORMER.preserves_schema is False
    assert ExtensionKind.BACKEND.preserves_schema is False
    # declares_schema: only TRANSFORMER owns its own composed schema — this is
    # what separates it from BACKEND, the other non-preserving kind.
    assert ExtensionKind.WRAPPER.declares_schema is False
    assert ExtensionKind.TRANSFORMER.declares_schema is True
    assert ExtensionKind.BACKEND.declares_schema is False


def test_extension_kind_preserves_output_shape():
    # OUTPUT-shape preservation, distinct from the INPUT-schema properties above:
    # a WRAPPER returns the wrapped result unchanged and a BACKEND returns the
    # same output shape via a swapped strategy, so both let a branch inherit the
    # base's output schema; a TRANSFORMER reshapes the result and must not.
    assert ExtensionKind.WRAPPER.preserves_output_shape is True
    assert ExtensionKind.TRANSFORMER.preserves_output_shape is False
    assert ExtensionKind.BACKEND.preserves_output_shape is True


def test_extension_kind_relocates_execution():
    # Only BACKEND moves the tool body to a worker process; WRAPPER and
    # TRANSFORMER execute the body in-process. The apply site keys the
    # stacking-order rule (locality-requiring extensions bind inside any
    # relocating one) off this property, never off the member.
    assert ExtensionKind.WRAPPER.relocates_execution is False
    assert ExtensionKind.TRANSFORMER.relocates_execution is False
    assert ExtensionKind.BACKEND.relocates_execution is True


def test_extension_factory_runtime_checkable():
    # ``runtime_checkable`` verifies only that ``__call__`` exists — it cannot
    # check the 3-argument shape — so a conforming factory passes isinstance.
    def factory(func: Any, name: str, description: str) -> Any:
        return func

    assert isinstance(factory, ExtensionFactory)


def test_extension_kind_is_its_string_value():
    # StrEnum: constructible from the plain string and serializes as it.
    assert ExtensionKind("wrapper") is ExtensionKind.WRAPPER
    assert ExtensionKind("transformer") is ExtensionKind.TRANSFORMER
    assert ExtensionKind("backend") is ExtensionKind.BACKEND
    assert ExtensionKind.WRAPPER == "wrapper"
    assert json.dumps({"kind": ExtensionKind.BACKEND}) == '{"kind": "backend"}'
