"""Response view shaping for a tool run: the full get view and the trimmed list view."""

from __future__ import annotations

import json
from typing import Any


def _run_view(run_id: str, record: dict[str, str]) -> dict[str, Any]:
    """The full GET view; ``result`` is parsed back from its stored JSON."""
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
    if "error" in record:
        view["error"] = record["error"]
    return view


def _list_view(run_id: str, record: dict[str, str]) -> dict[str, Any]:
    """The list view — id/tool name/status/timestamps only, never ``result``/``error``."""
    view: dict[str, Any] = {
        "run_id": run_id,
        "tool_name": record["tool_name"],
        "status": record["status"],
        "started_at": record["started_at"],
    }
    if "finished_at" in record:
        view["finished_at"] = record["finished_at"]
    return view
