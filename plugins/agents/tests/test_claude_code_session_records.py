"""The durable ``.runner`` records ``claude_code`` reads back: an absent record reads as none, any other read
failure raises."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.sandbox import SandboxError, SandboxFileNotFoundError

from tai42_agents.claude_code.session_records import read_session_id, read_terminal_record


class _Unreadable:
    """A session whose every file read fails with ``error``; only ``get_file`` is read."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def get_file(self, _path: str) -> bytes:
        raise self._error


def _unreadable(error: Exception) -> Any:
    return _Unreadable(error)


def _read_both(session: Any) -> tuple[Any, Any]:
    async def _both() -> tuple[Any, Any]:
        return await read_session_id(session), await read_terminal_record(session, "step")

    return asyncio.run(_both())


def test_absent_records_read_as_none() -> None:
    assert _read_both(_unreadable(SandboxFileNotFoundError(".runner/session_id"))) == (None, None)


def test_a_failed_session_id_read_raises() -> None:
    with pytest.raises(SandboxError, match="volume unreachable"):
        asyncio.run(read_session_id(_unreadable(SandboxError("volume unreachable"))))


def test_a_failed_terminal_record_read_raises() -> None:
    with pytest.raises(SandboxError, match="volume unreachable"):
        asyncio.run(read_terminal_record(_unreadable(SandboxError("volume unreachable")), "step"))
