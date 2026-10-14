"""The LangGraph setup lock against a scripted connection: it polls, releases, and closes on a failed release."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("psycopg")

from tai42_kit.llm import _langgraph_postgres
from tai42_kit.llm._langgraph_postgres import serialised_setup


class _Cursor:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def fetchone(self) -> Any:
        return self._row


class _Conn:
    def __init__(self, try_results: list[Any], *, unlock_error: BaseException | None = None) -> None:
        self._try_results = list(try_results)
        self._unlock_error = unlock_error
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def execute(self, query: str, params: tuple[Any, ...]) -> _Cursor:
        self.statements.append((query, params))
        if "pg_try_advisory_lock" in query:
            return _Cursor(self._try_results.pop(0))
        if self._unlock_error is not None:
            raise self._unlock_error
        return _Cursor({"pg_advisory_unlock": True})

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_poll_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_langgraph_postgres, "_SETUP_LOCK_POLL_SECONDS", 0)


async def test_polls_until_the_lock_is_free_then_releases_it() -> None:
    conn = _Conn([{"locked": False}, {"locked": False}, {"locked": True}])
    async with serialised_setup(conn, "checkpoint_migrations"):  # type: ignore[arg-type]
        assert len(conn.statements) == 3
    assert [q.split("(")[0] for q, _ in conn.statements] == [
        "SELECT pg_try_advisory_lock",
        "SELECT pg_try_advisory_lock",
        "SELECT pg_try_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert {p for _, p in conn.statements} == {("checkpoint_migrations",)}
    assert conn.closed is False


async def test_a_tuple_row_is_read_by_position() -> None:
    conn = _Conn([(False,), (True,)])
    async with serialised_setup(conn, "store_migrations"):  # type: ignore[arg-type]
        pass
    assert len(conn.statements) == 3


async def test_the_lock_is_released_when_the_body_raises() -> None:
    conn = _Conn([{"locked": True}])
    with pytest.raises(ValueError, match="setup failed"):
        async with serialised_setup(conn, "checkpoint_migrations"):  # type: ignore[arg-type]
            raise ValueError("setup failed")
    assert conn.statements[-1][0].startswith("SELECT pg_advisory_unlock")
    assert conn.closed is False


async def test_a_failed_release_closes_the_connection_and_raises() -> None:
    conn = _Conn([{"locked": True}], unlock_error=OSError("connection lost"))
    with pytest.raises(OSError, match="connection lost"):
        async with serialised_setup(conn, "checkpoint_migrations"):  # type: ignore[arg-type]
            pass
    assert conn.closed is True


async def test_no_row_from_the_lock_query_raises() -> None:
    conn = _Conn([None])
    with pytest.raises(RuntimeError, match="returned no row for the 'checkpoint_migrations' setup lock"):
        async with serialised_setup(conn, "checkpoint_migrations"):  # type: ignore[arg-type]
            pass
