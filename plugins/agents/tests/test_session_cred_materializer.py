"""The one session-cred rule both sandbox agents materialize through: ``materialize_session_creds``."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest
from pydantic import SecretStr
from tai42_contract.connectors import ResolvedConnectionAuth

from tai42_agents._internal.session_cred import (
    BearerCred,
    ConnectionCred,
    SessionCredError,
    SessionCredMaterial,
    StaticCred,
    materialize_session_creds,
)

from .conftest import APP, RecordingConnectors


@pytest.fixture(autouse=True)
def _clean_connectors() -> Any:
    connectors: RecordingConnectors = APP.connectors
    connectors.resolved.clear()
    connectors.calls.clear()
    yield
    connectors.resolved.clear()
    connectors.calls.clear()


def _resolve(**channels: Any) -> None:
    APP.connectors.resolved["c"] = ResolvedConnectionAuth(**channels)


_Delivery = Literal["bearer", "env"]


def _conn(delivery: _Delivery = "bearer", *, required: bool = True, env_name: str = "CONN_KEY") -> ConnectionCred:
    return ConnectionCred(
        env_name=env_name, connection_id="c", provider_id="p", sub_service="s", delivery=delivery, required=required
    )


def _materialize(*creds: Any) -> SessionCredMaterial:
    return asyncio.run(materialize_session_creds(list(creds)))


def _plain(env: dict[str, SecretStr]) -> dict[str, str]:
    return {name: value.get_secret_value() for name, value in env.items()}


def test_env_delivery_puts_the_raw_token_under_the_env_name_and_the_env_channel_under_its_names() -> None:
    _resolve(access_token=SecretStr("TOK"), env={"SVC_REGION": SecretStr("eu")})
    material = _materialize(_conn("env"))
    assert _plain(material.env) == {"SVC_REGION": "eu", "CONN_KEY": "TOK"}
    assert material.bearer == []


def test_bearer_delivery_puts_the_token_in_a_bearer_file_and_the_env_channel_in_the_env() -> None:
    _resolve(access_token=SecretStr("TOK"), env={"SVC_REGION": SecretStr("eu")})
    material = _materialize(_conn("bearer"))
    assert _plain(material.env) == {"SVC_REGION": "eu"}
    assert len(material.bearer) == 1
    assert material.bearer[0].env_name == "CONN_KEY"
    assert material.bearer[0].file_body() == "Authorization: Bearer TOK\n"


def test_bearer_delivery_carries_headers_beside_the_token() -> None:
    _resolve(access_token=SecretStr("TOK"), headers={"X-Api-Version": SecretStr("2024-08")})
    material = _materialize(_conn("bearer"))
    assert material.bearer[0].file_body() == "Authorization: Bearer TOK\nX-Api-Version: 2024-08\n"


def test_bearer_delivery_with_headers_and_no_token_writes_only_the_header_lines() -> None:
    _resolve(headers={"X-Key": SecretStr("k")})
    material = _materialize(_conn("bearer"))
    assert material.bearer == [BearerCred(env_name="CONN_KEY", token=None, headers=material.bearer[0].headers)]
    assert material.bearer[0].file_body() == "X-Key: k\n"


def test_headers_under_env_delivery_are_refused() -> None:
    _resolve(access_token=SecretStr("TOK"), headers={"X-Key": SecretStr("k")})
    with pytest.raises(
        SessionCredError,
        match=r"connection cred 'CONN_KEY' resolved HTTP headers, which delivery='env' cannot carry; "
        r"use delivery='bearer'",
    ):
        _materialize(_conn("env"))


@pytest.mark.parametrize("resolved", [None, ResolvedConnectionAuth()])
@pytest.mark.parametrize("delivery", ["env", "bearer"])
def test_a_required_cred_resolving_to_nothing_is_refused(resolved: Any, delivery: _Delivery) -> None:
    APP.connectors.resolved["c"] = resolved
    with pytest.raises(
        SessionCredError,
        match=r"required connection cred 'CONN_KEY' resolved to no usable credential for the current caller "
        r"\(connection 'c'\)",
    ):
        _materialize(_conn(delivery))


@pytest.mark.parametrize("delivery", ["env", "bearer"])
def test_a_non_required_cred_resolving_to_nothing_injects_nothing(delivery: _Delivery) -> None:
    APP.connectors.resolved["c"] = None
    material = _materialize(_conn(delivery, required=False))
    assert material.env == {}
    assert material.env_names == []
    assert material.bearer == []


def test_a_static_cred_rides_the_env() -> None:
    material = _materialize(StaticCred(env_name="STATIC_KEY", value=SecretStr("v")))
    assert _plain(material.env) == {"STATIC_KEY": "v"}
    assert material.env_names == ["STATIC_KEY"]


def test_env_names_keep_insertion_order() -> None:
    _resolve(access_token=SecretStr("TOK"), env={"SVC_REGION": SecretStr("eu")})
    material = _materialize(
        StaticCred(env_name="FIRST", value=SecretStr("1")),
        _conn("env"),
        StaticCred(env_name="LAST", value=SecretStr("2")),
    )
    assert material.env_names == ["FIRST", "SVC_REGION", "CONN_KEY", "LAST"]


def test_secret_values_cover_every_injected_value() -> None:
    _resolve(access_token=SecretStr("TOK"), env={"SVC_REGION": SecretStr("eu")}, headers={"X-Key": SecretStr("k")})
    material = _materialize(StaticCred(env_name="S", value=SecretStr("static")), _conn("bearer"))
    assert sorted(material.secret_values()) == sorted(["static", "eu", "TOK", "k"])


def test_resolution_reads_the_operator_connection_fields() -> None:
    _resolve(access_token=SecretStr("TOK"))
    _materialize(_conn("bearer"))
    assert APP.connectors.calls == [("c", "p", "s")]


# --- parity: both sandbox agents materialize the same cred list the same way ----


def test_both_agents_materialize_the_same_session_env_and_bearer_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from tai42_agents.claude_code.credentials import resolve_creds
    from tai42_agents.claude_code.settings import ANTHROPIC_API_KEY_ENV, ClaudeCodeSettings
    from tai42_agents.langchain_deep_agent import session as deep_session
    from tai42_agents.langchain_deep_agent.session import DeepAgentSession
    from tai42_agents.langchain_deep_agent.settings import LangchainDeepAgentSettings

    APP.connectors.resolved["env-conn"] = ResolvedConnectionAuth(
        access_token=SecretStr("TOK"), env={"SVC_REGION": SecretStr("eu")}
    )
    APP.connectors.resolved["bearer-conn"] = ResolvedConnectionAuth(
        access_token=SecretStr("BTOK"), env={"SVC_ZONE": SecretStr("z1")}, headers={"X-Api": SecretStr("2")}
    )
    creds: list[Any] = [
        StaticCred(env_name="STATIC_KEY", value=SecretStr("static")),
        ConnectionCred(env_name="CONN_KEY", connection_id="env-conn", provider_id="p", sub_service="s", delivery="env"),
        ConnectionCred(env_name="GH_TOKEN", connection_id="bearer-conn", provider_id="p", sub_service="s"),
    ]
    digest = "registry.example/img@sha256:" + "c" * 64

    claude = asyncio.run(
        resolve_creds(ClaudeCodeSettings(session_image=digest, api_key=SecretStr("model"), creds=creds))  # type: ignore[call-arg]
    )
    claude_env = _plain(claude.env)
    assert claude_env.pop(ANTHROPIC_API_KEY_ENV) == "model"
    claude_files = {cred.env_name: cred.file_body() for cred in claude.bearer}

    deep_settings = LangchainDeepAgentSettings(session_image=digest, creds=creds)
    monkeypatch.setattr(deep_session, "langchain_deep_agent_settings", lambda: deep_settings)
    captured: dict[str, Any] = {}
    real_build = deep_session.build_policied_spec

    def capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(deep_session, "build_policied_spec", capture)
    drive = asyncio.run(DeepAgentSession.acquire(thread_id=None))
    creds_dir = Path(drive.session.workspace_path) / ".creds"
    deep_files = {path.name: path.read_text() for path in creds_dir.iterdir()}

    assert (
        claude_env
        == _plain(captured["env"])
        == {
            "STATIC_KEY": "static",
            "SVC_REGION": "eu",
            "CONN_KEY": "TOK",
            "SVC_ZONE": "z1",
        }
    )
    assert claude_files == deep_files == {"GH_TOKEN": "Authorization: Bearer BTOK\nX-Api: 2\n"}
