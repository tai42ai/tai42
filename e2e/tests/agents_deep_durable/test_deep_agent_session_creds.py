"""Operator SERVICE creds reach the deep agent's sandbox shell (§B4), and an identity-less door
fails closed.

The ``langchain_deep_agent`` ``creds`` setting injects the operator's session creds into the
CLEAN sandbox session — the deep-agent analogue of the coding agent's session-creds reach. The
MODEL credential is deliberately NOT among them (the deep agent's model call runs SERVER-side via
``get_llm_async``); only service creds enter the session.

Two deterministic legs, both over the fake sandbox with no real vendor key:

* STATIC-env channel — a ``StaticCred`` is baked into the CLEAN session env at create; the deep
  agent's built-in ``execute`` shell reads ``$E2E_SVC_TOKEN`` back out of its process env, so the
  KNOWN constant round-trips through the shell output (it reached the session env). This is the
  identity-AGNOSTIC channel (a static value needs no per-caller resolution), so it lands even on
  the auth-off run.

* IDENTITY-LESS FAIL-CLOSE — a connection-reference ``ConnectionCred`` on a door with NO bound
  execution identity (the auth-off MCP run) causes a LOUD RAISE at session setup: it is
  ``resolve_connection_auth`` that refuses when no execution identity is bound (the skeleton seam's
  fail-close), NOT the agent detecting identity itself, and NO cred is injected (never a silent
  drop). The refreshable ``delivery="bearer"`` ``{ws}/.creds`` materialization + terminal-exit
  scrub, and the identity-BOUND positive injection, are proven deterministically in PLAN_4's §B4
  unit tests (they need a bound execution identity + a live connection, an auth-on + connectors
  composition no durable e2e stack carries).

Each leg boots a FRESH function-scoped stack carrying its own ``creds`` env (a connection-ref
raises for every run, so it cannot share a stack with the static positive).
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_deep_agent_durable_stack
from tai42_e2e.stack import TaiStack

from ._support import AGENT, DETERMINISTIC_MARKS

pytestmark = DETERMINISTIC_MARKS

_CRED_ENV = "TAI_AGENTS_LANGCHAIN_DEEP_CREDS"
# The KNOWN constant a StaticCred bakes into the session env — asserted to round-trip back out of
# the deep agent's shell. Baked at boot, so it is a fixed literal (never a per-test uniq), and it
# appears in no prompt, so its only path to the model is the shell reading it from the env.
_STATIC_TOKEN = "e2e-svc-static-8f2c41d9"


def _creds_overrides(*specs: dict) -> dict[str, str]:
    return {_CRED_ENV: json.dumps(list(specs))}


async def test_static_cred_reaches_the_deep_agent_sandbox_shell(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(
        build_deep_agent_durable_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        env_overrides=_creds_overrides({"kind": "static", "env_name": "E2E_SVC_TOKEN", "value": _STATIC_TOKEN}),
        allocate_checkpoint_db=True,
    )
    llm_stub.reset()
    # The shell reads the injected service cred back out of its clean session env.
    llm_stub.script(
        [
            {"tool_call": {"name": "execute", "arguments": {"command": 'printf %s "$E2E_SVC_TOKEN"'}}},
            {"content": "done"},
        ]
    )
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.call_tool(AGENT, {"user_message": uniq("read the token")}, retry_on_reloading=True)

    read_back = [m for m in llm_stub.requests[-1]["messages"] if m.get("role") == "tool"][-1]
    assert _STATIC_TOKEN in json.dumps(read_back), (
        f"the static service cred did not reach the deep agent's session env: {read_back}"
    )


async def test_connection_ref_on_an_identity_less_door_fails_closed(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(
        build_deep_agent_durable_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        env_overrides=_creds_overrides(
            {
                "kind": "connection",
                "env_name": "E2E_SVC_TOKEN",
                "connection_id": "e2e-conn",
                "provider_id": "e2e_idp",
                "sub_service": "default",
            }
        ),
        allocate_checkpoint_db=True,
    )
    llm_stub.reset()
    # A completion is queued but never consumed: session setup raises the fail-close BEFORE any
    # model call.
    llm_stub.script([{"content": "unreached"}])
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(AGENT, {"user_message": uniq("q")}, raise_on_error=False, retry_on_reloading=True)

    assert result.is_error, "a connection-ref cred on an identity-less door did not fail"
    text = " ".join(getattr(p, "text", "") for p in result.content)
    # The refusal is the skeleton seam's own fail-close (no execution identity bound), not the
    # agent's — and it fires at session setup, before any model call.
    assert "execution identity" in text, f"the failure was not the identity-less fail-close: {text}"
    assert not llm_stub.requests, f"the run reached the model despite the cred fail-close: {llm_stub.requests}"
