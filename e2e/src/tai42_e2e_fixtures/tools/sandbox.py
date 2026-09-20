"""The sandbox lifecycle probe: drives create/exec/put/get/touch/info/list/reap/
destroy through the ONE acquisition chokepoint against a process-level live-session
registry. Registers at import via ``@tai42_app.tools.tool``."""

from __future__ import annotations

import json
import os

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings

# The process-level live-session registry the sandbox probe drives a multi-step lifecycle
# through: ``create`` returns a ``session_id`` a later ``exec`` / ``put`` / ``get`` addresses.
# A session lives in ONE process, so the sandbox-probe suites ride the single-worker
# ``build_sandbox_stack`` where every probe call lands in the same server process.
_SANDBOX_SESSIONS: dict[str, object] = {}

# The default digest-pinned image reference the probe requests (inert under the fake/local
# providers, but the model demands a digest, never a bare tag).
_PROBE_IMAGE = "img@sha256:" + "0" * 64


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_sandbox_probe(
    op: str,
    *,
    session_id: str | None = None,
    workspace_key: str = "e2e-probe",
    durability: str = "ephemeral",
    network: str = "egress",
    image: str = _PROBE_IMAGE,
    argv: list[str] | None = None,
    path: str | None = None,
    data: str | None = None,
    ttl_seconds: int = 300,
    timeout_seconds: float = 30,
    record_key: str | None = None,
) -> dict:
    """Drive one sandbox lifecycle step through the ONE acquisition chokepoint.

    Acquires the provider via ``tai42_app.sandboxes.require_sandbox()`` (so a provider-less
    stack surfaces the typed loud ``SandboxUnavailableError`` through this tool's OWN
    error path — never a 501 route) and performs ``op``:
    ``create`` / ``exec`` / ``put`` / ``get`` / ``touch`` / ``info`` / ``list`` / ``reap`` /
    ``destroy``. The outcome is returned AND, when ``record_key`` is set, RPUSHed onto
    ``e2e:rec:{record_key}`` so a spec reads it back through ``stack.records``.
    """
    from tai42_contract.sandbox import SandboxSessionSpec

    sandbox = tai42_app.sandboxes.require_sandbox()

    if op == "create":
        spec = SandboxSessionSpec(
            image=image,
            workspace_key=workspace_key,
            durability=durability,  # type: ignore[arg-type]  # validated by the model
            network=network,  # type: ignore[arg-type]  # validated by the model
            ttl_seconds=ttl_seconds,
        )
        session = await sandbox.create_session(spec)
        _SANDBOX_SESSIONS[session.id] = session
        outcome = {"session_id": session.id, "workspace_path": session.workspace_path}
    elif op == "list":
        infos = await sandbox.list_sessions()
        outcome = {"sessions": [info.id for info in infos]}
    elif op == "reap":
        outcome = {"reaped": await sandbox.reap()}
    else:
        session = _require_probe_session(session_id)
        outcome = await _run_session_op(
            session, op, session_id, argv=argv, path=path, data=data, timeout_seconds=timeout_seconds
        )

    if record_key is not None:
        await _record_probe(record_key, outcome)
    return {"op": op, "pid": os.getpid(), **outcome}


async def _run_session_op(
    session: object,
    op: str,
    session_id: str | None,
    *,
    argv: list[str] | None,
    path: str | None,
    data: str | None,
    timeout_seconds: float,
) -> dict:
    """Run one op against an already-acquired live probe session, returning its outcome."""
    if op == "exec":
        result = await session.exec(argv or [], timeout_seconds=timeout_seconds)  # type: ignore[attr-defined]
        return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
    if op == "put":
        await session.put_file(_require(path, "path"), (data or "").encode("utf-8"))  # type: ignore[attr-defined]
        return {"put": path}
    if op == "get":
        payload = await session.get_file(_require(path, "path"))  # type: ignore[attr-defined]
        return {"data": payload.decode("utf-8", "replace")}
    if op == "touch":
        await session.touch()  # type: ignore[attr-defined]
        return {"touched": True}
    if op == "info":
        info = await session.info()  # type: ignore[attr-defined]
        return {"session_id": info.id, "workspace_path": info.workspace_path, "durability": info.durability}
    if op == "destroy":
        await session.destroy()  # type: ignore[attr-defined]
        _SANDBOX_SESSIONS.pop(session_id or "", None)
        return {"destroyed": True}
    raise ValueError(f"unknown sandbox probe op {op!r}")


def _require_probe_session(session_id: str | None) -> object:
    if session_id is None:
        raise ValueError("this sandbox probe op requires a session_id from a prior create")
    session = _SANDBOX_SESSIONS.get(session_id)
    if session is None:
        raise ValueError(f"no live probe session {session_id!r} in this process")
    return session


def _require(value: str | None, name: str) -> str:
    if value is None:
        raise ValueError(f"this sandbox probe op requires {name!r}")
    return value


async def _record_probe(key: str, outcome: dict) -> None:
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({"value": outcome, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))
