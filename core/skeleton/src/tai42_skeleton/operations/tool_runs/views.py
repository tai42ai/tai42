"""Response view shaping for a tool run: the full get view and the trimmed list view."""

from __future__ import annotations

import json
from typing import Any


def _run_view(run_id: str, record: dict[str, str]) -> dict[str, Any]:
    """The full GET view; ``result``, ``outcome`` and ``resumed_interactions`` are parsed back from stored JSON.

    ``outcome`` is present only on a ``failed`` record whose run reached a failed terminal: the
    driver's outcome, whole. ``resumed_interactions`` is the parked interaction ids the run resumed or took while it
    executed (written on every terminal record; ``[]`` when it resumed nothing).
    """
    view: dict[str, Any] = {
        "run_id": run_id,
        "tool_name": record["tool_name"],
        "status": record["status"],
        "started_at": record["started_at"],
    }
    if "finished_at" in record:
        view["finished_at"] = record["finished_at"]
    if "result" in record:
        view["result"] = json.loads(record["result"])
    if "resumed_interactions" in record:
        view["resumed_interactions"] = json.loads(record["resumed_interactions"])
    if "error" in record:
        view["error"] = record["error"]
    if "outcome" in record:
        view["outcome"] = json.loads(record["outcome"])
    return view


def _list_view(run_id: str, record: dict[str, str]) -> dict[str, Any]:
    """The list view — id/tool name/status/timestamps only, never ``result``/``error``/``outcome``."""
    view: dict[str, Any] = {
        "run_id": run_id,
        "tool_name": record["tool_name"],
        "status": record["status"],
        "started_at": record["started_at"],
    }
    if "finished_at" in record:
        view["finished_at"] = record["finished_at"]
    return view
