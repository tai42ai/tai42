"""Checkpoints router: the enveloped sweep result and the admin-only fence.

The handler is driven directly (the router-test pattern) over the kit's in-process
checkpoint store. The sweep is a
deployment-wide destructive memory purge, so its route is ``action="fenced"`` —
admin only, denied to every non-admin regardless of granted level.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from tai42_skeleton.routers import checkpoints as router


def _req() -> Request:
    return cast(Request, SimpleNamespace(path_params={}, reload_gated=False))


def _json(resp: Any) -> dict:
    return json.loads(bytes(resp.body))


async def test_sweep_route_envelopes_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.llm.checkpoint import liveness
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setattr(liveness, "_filters", {})
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "memory")
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        ledger = await registry.ledger("memory", None)
        await ledger.mark(["bridge:sms:old"], datetime.now(UTC) - timedelta(days=2))

        resp = await router.sweep_checkpoints(_req())
        assert resp.status_code == 200
        body = _json(resp)
        assert body["data"]["swept_count"] == 1
        assert body["data"]["finished_swept"] == ["bridge:sms:old"]
        assert body["data"]["waiting_swept"] == []
        assert body["data"]["spared"] == []
        assert body["data"]["provider"] == "memory"
    finally:
        await registry.close_all()
        reset_all_settings()


def test_sweep_route_is_admin_fenced() -> None:
    from tai42_skeleton.access_control.role_gate import (
        DenialCause,
        grant_map_admits,
        reset_route_index,
        resolve_route_meta,
    )

    reset_route_index()
    meta = resolve_route_meta("/api/checkpoints/sweep", "POST")
    assert meta is not None
    assert meta.action == "fenced"
    # No per-tag level opens a fenced route: even a role granted write on the tag is denied.
    allowed, cause = grant_map_admits(meta, "POST", {"checkpoints": "write"})
    assert allowed is False
    assert cause is DenialCause.HARD_FENCE
