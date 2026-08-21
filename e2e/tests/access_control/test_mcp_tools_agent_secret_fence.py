"""The F1 secret fence, proven END TO END through the real per-role wiring.

``mcp_tools_agent``'s ``inject_env`` (host env into an MCP server) is refused unless the
caller is authorized to read the platform's secrets. That authorization is NOT set by the
agent: the auth backend decides the admin/secret discriminator per identity, the skeleton
:class:`~tai42_skeleton.access_control.middleware.ResourceGuardMiddleware` binds it onto the
``caller_may_read_secrets`` contextvar for the request, and the agent reads it back and
refuses. The plugin unit test sets that contextvar DIRECTLY, so it cannot prove the
middleware wires it per role. This leg drives the whole chain over HTTP:

* An ``editor`` session — a non-admin role holding ``agents:write`` — invokes the agent
  with ``inject_env=True`` over the role-differentiated ``POST /api/agents/{name}/runs``
  door. The editor PASSES route authz (``agents:write`` reaches the write door, the editor
  base tier admits a non-``/api/auth`` write), REACHES the agent, and is refused by the
  agent's own secret fence — a terminal ``stream.error`` carrying the ``inject_env is
  restricted`` message, with no run ever entering execution. A ``403`` at the door instead
  would mean the ROUTE fence stopped the editor before the tool; asserting ``200`` + the
  fence message is what pins the refusal to F1's per-role wiring.
* An ``admin`` session — the admin discriminator the middleware stamps secret-capable —
  invokes the SAME call and is ALLOWED: the fence passes, the agent loads its tools from
  the stack's own ``/mcp`` and the scripted LLM drives the run to its final. Same door,
  same body, opposite outcome — so the difference is provably the identity's secret
  capability, threaded from the auth backend through the middleware to the agent.

The plain ``POST /api/run-tool`` door is ``action=fenced`` (admin-only), so an editor is
denied THERE before ever reaching the agent — it cannot exercise F1's per-role wiring. The
existing ``tests/agents/test_mcp_tools_agent.py`` invokes over the MCP EDGE as the default
admin identity and never drives the editor-refused path. Hence this role-differentiated
HTTP run door with a real editor token.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.mcp import mcp_url
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._rbac_support import create_role, create_user_with_role

# The scripted-stub round-trip is the LLM MOCK leg; the real-provider leg runs on the e2e
# creds host, not in CI. Inert in the default mock run (``is_real('llm')`` is False), so the
# module collects byte-for-byte today's on the default matrix.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
)

# The refusal the agent raises when a non-secret-capable caller asks for ``inject_env``
# (``mcp_tools_agent._require_secret_capability``), surfaced by the run door as a terminal
# ``stream.error`` frame — the load-bearing signal that F1 refused this identity.
_FENCE_REFUSAL = "inject_env is restricted to a caller authorized to read secrets"


async def _post_agent_run(client: ApiClient, agent: str, body: dict[str, object]) -> list[dict[str, object]]:
    """POST an agent run and return its parsed SSE frames (one dict per ``data:`` line).

    The run door answers ``200`` with a finite ``text/event-stream`` that always
    terminates (``stream.end`` or ``stream.error``), so the buffered body carries every
    frame. A boot-time reload-gate ``503`` is polled past, the retry every real client
    applies; any other non-``200`` is a hard failure surfaced with its body."""
    for _ in range(20):
        resp = await client.request_raw("POST", f"/api/agents/{agent}/runs", json=body)
        if resp.status_code == 503 and "retry-after" in resp.headers:
            continue
        assert resp.status_code == 200, f"agent run door -> {resp.status_code} (expected 200); body: {resp.text}"
        return _parse_sse(resp.text)
    raise AssertionError("agent run door never left the boot reload gate")


def _parse_sse(text: str) -> list[dict[str, object]]:
    """The JSON payloads of every ``data:`` frame in an SSE body (keep-alive comment lines
    carry no ``data:`` and are skipped)."""
    frames: list[dict[str, object]] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            frames.append(json.loads(line[len("data:") :].strip()))
    return frames


async def test_inject_env_refused_for_editor_allowed_for_admin_over_the_run_door(
    agents_authz_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack = agents_authz_stack
    admin = stack.api(port=stack.port_a)

    # An editor role that CAN reach the agents run door: ``agents:write`` satisfies the
    # write door, so the ONLY thing that can refuse the run is the agent's own secret
    # fence — never a missing grant.
    role_name = uniq("agents-editor")
    await create_role(admin, name=role_name, base_tier="editor", grants={"agents": "write"})
    _editor_id, editor_token = await create_user_with_role(stack, admin, uniq, role=role_name)
    editor = stack.api(port=stack.port_b).with_token(editor_token)

    # An admin-role session: the middleware stamps it secret-capable (a condition-free
    # ``"*"`` policy — the admin discriminator), so the same call clears the fence.
    _admin_id, admin_token = await create_user_with_role(stack, admin, uniq, role="admin")
    admin_session = stack.api(port=stack.port_b).with_token(admin_token)

    # The agent's tools come from the stack's OWN ``/mcp`` (the existing agents e2e's stub
    # MCP source — no external server); the access-controlled endpoint authenticates the
    # back-connect with the stack's root key. ``inject_env`` is the fenced primitive under
    # test; ``env_allowlist`` names one var certain to exist in the worker process.
    mcp_config = {
        "mcpServers": {
            "local": {
                "url": mcp_url(stack.host, stack.port_a),
                "headers": {"Authorization": f"Bearer {stack.auth_token}"},
            }
        }
    }
    run_body = {"mcp_config": mcp_config, "inject_env": True, "env_allowlist": ["PATH"], "user_message": "go"}

    # EDITOR: reaches the tool (200 SSE, not a 403 route fence) and is refused by F1 — the
    # terminal frame is a ``stream.error`` carrying the fence message, and NO run event or
    # ``stream.end`` is emitted (the run never entered execution).
    editor_frames = await _post_agent_run(editor, "mcp_tools_agent", run_body)
    errors = [f for f in editor_frames if f.get("type") == "stream.error"]
    assert errors, f"editor run produced no stream.error; frames: {editor_frames}"
    assert _FENCE_REFUSAL in str(errors[-1].get("message", "")), (
        f"editor refusal was not the F1 secret fence: {errors[-1]}"
    )
    assert not [f for f in editor_frames if f.get("type") in {"stream.end", "message_final"}], (
        f"editor run reached execution despite the fence: {editor_frames}"
    )

    # ADMIN: the SAME call clears the fence and the run reaches execution — the scripted
    # single-turn final drains to a ``message_final`` + ``stream.end`` (tools loaded over
    # the authenticated ``/mcp``, one LLM round-trip), and NO F1 refusal appears.
    final_text = f"fence-cleared-{uniq('final')}"
    llm_stub.reset()
    llm_stub.script([{"content": final_text}])
    admin_frames = await _post_agent_run(admin_session, "mcp_tools_agent", run_body)
    assert not [
        f for f in admin_frames if f.get("type") == "stream.error" and _FENCE_REFUSAL in str(f.get("message", ""))
    ], f"admin was wrongly refused by the F1 secret fence: {admin_frames}"
    assert any(f.get("type") == "stream.end" for f in admin_frames), (
        f"admin run did not reach a clean stream.end: {admin_frames}"
    )
    assert final_text in json.dumps(admin_frames), (
        f"admin run's scripted final never surfaced (execution not reached): {admin_frames}"
    )
