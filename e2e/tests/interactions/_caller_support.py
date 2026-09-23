"""Shared drivers for the caller-ask e2e suites: the subject shape, the tool-runs submit
door, and the run-status polls the specs read a detached run's outcome through."""

from __future__ import annotations

from typing import Any

from tai42_e2e import wait_for_async

_TERMINAL = {"succeeded", "failed"}


def subject(key: str) -> dict[str, str]:
    """A ``person``-kind subject scope both a parking run and its resumer address."""
    return {"target_kind": "tool", "target_name": "held_run", "kind": "person", "key": key}


async def submit(api: Any, tool_name: str, arguments: dict[str, Any], subj: dict[str, str] | None = None) -> str:
    """Submit ``tool_name`` as a detached run under ``subj`` and return its run id."""
    body: dict[str, Any] = {"tool_name": tool_name, "arguments": arguments}
    if subj is not None:
        body["subject"] = subj
    submitted = await api.post("/api/tool-runs", json=body, expect=202, retry_on_reloading=True)
    return submitted["run_id"]


async def await_status(api: Any, run_id: str, status: str, *, deadline: float = 20.0) -> dict[str, Any]:
    """Poll ``run_id`` until it reaches ``status`` and return the run view."""

    async def _reached() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == status else None

    return await wait_for_async(_reached, deadline=deadline, message=f"run {run_id} never reached {status}")


async def await_terminal(api: Any, run_id: str, *, deadline: float = 20.0) -> dict[str, Any]:
    """Poll ``run_id`` until it reaches a terminal (succeeded/failed) and return the run view."""

    async def _reached() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] in _TERMINAL else None

    return await wait_for_async(_reached, deadline=deadline, message=f"run {run_id} never reached a terminal")


async def await_result(api: Any, run_id: str, *, deadline: float = 20.0) -> Any:
    """Poll ``run_id`` to ``succeeded`` and return its result payload."""
    view = await await_status(api, run_id, "succeeded", deadline=deadline)
    return view["result"]


async def caller_ask_id(api: Any, subj: dict[str, str], *, deadline: float = 15.0) -> str:
    """Submit ``caller_list`` on ``subj`` and return the id of the single caller ask parked there."""

    async def _one() -> str | None:
        run_id = await submit(api, "caller_list", {}, subj)
        entries = await await_result(api, run_id)
        return entries[0]["id"] if len(entries) == 1 else None

    return await wait_for_async(_one, deadline=deadline, message="a single caller ask never appeared on the subject")
