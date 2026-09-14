"""Shared fixtures and helpers for the synthesized agent ``run`` tool tests."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def _plain_tools_manifest(*names: str) -> Manifest:
    return Manifest.model_validate(
        {"tools": [{"title": "tools", "module": "tests.agent._turn_budget_fixtures", "include": list(names)}]}
    )


@pytest.fixture
def set_turn_timeout(monkeypatch: pytest.MonkeyPatch):
    # Set the turn budget via env, drop the settings cache so the accessor rereads it,
    # and drop it again on teardown so the value cannot leak into later tests.
    def _set(value: str) -> None:
        monkeypatch.setenv("TAI_TURN_TIMEOUT_SECONDS", value)
        reset_all_settings()

    yield _set
    reset_all_settings()


def _fixture_flag(module: str, name: str) -> Any:
    # A fixtures module is popped from sys.modules and re-imported on each app_context
    # enter, so a tool/agent that ran mutates the LIVE module object; read the flag off
    # that one, not a stale import binding.
    return getattr(sys.modules[module], name)


def _budget_flag(name: str) -> Any:
    return _fixture_flag("tests.agent._turn_budget_fixtures", name)


class _LogCapture(logging.Handler):
    """Captures each emitted record's FULLY formatted text — message plus the
    exception traceback ``logger.exception`` attaches — so a leaked secret in either
    is detectable."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(self.format(record))
