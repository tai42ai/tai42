"""Unit tests for the passive per-MCP dispatch-health store."""

from __future__ import annotations

import threading
import time

import pytest

from tai42_skeleton.tools import mcp_health


@pytest.fixture(autouse=True)
def _clear_store():
    mcp_health._HEALTH.clear()
    yield
    mcp_health._HEALTH.clear()


def test_never_called_snapshot_is_all_empty():
    assert mcp_health.snapshot("Utilities") == {
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
        "failing_since": None,
    }


def test_success_records_timestamp_and_clears_failure_run():
    mcp_health.record_failure("Utilities", RuntimeError("down"))
    mcp_health.record_success("Utilities")

    health = mcp_health.snapshot("Utilities")
    assert health["last_success"] is not None
    assert health["last_error"] is None
    assert health["consecutive_failures"] == 0
    assert health["failing_since"] is None


def test_failures_accumulate_and_pin_failing_since():
    mcp_health.record_failure("Utilities", RuntimeError("one"))
    first_since = mcp_health.snapshot("Utilities")["failing_since"]
    mcp_health.record_failure("Utilities", ValueError("two"))

    health = mcp_health.snapshot("Utilities")
    assert health["consecutive_failures"] == 2
    # failing_since stays pinned to the run's FIRST error.
    assert health["failing_since"] == first_since
    assert health["last_error"]["type"] == "ValueError"
    assert health["last_error"]["message"] == "two"
    assert health["last_error"]["at"] is not None


def test_success_between_failures_starts_a_fresh_run():
    mcp_health.record_failure("Utilities", RuntimeError("one"))
    mcp_health.record_success("Utilities")
    mcp_health.record_failure("Utilities", RuntimeError("two"))

    health = mcp_health.snapshot("Utilities")
    assert health["consecutive_failures"] == 1
    assert health["failing_since"] is not None
    assert health["last_success"] is not None


def test_snapshot_returns_a_copy():
    mcp_health.record_failure("Utilities", RuntimeError("down"))
    snap = mcp_health.snapshot("Utilities")
    snap["last_error"]["type"] = "MUTATED"

    assert mcp_health.snapshot("Utilities")["last_error"]["type"] == "RuntimeError"


def test_failure_message_redacts_url_embedded_credentials():
    text = "Client error '401 Unauthorized' for url 'https://user:secret@host/path?token=abc&x=1' - see docs"
    mcp_health.record_failure("Utilities", RuntimeError(text))

    message = mcp_health.snapshot("Utilities")["last_error"]["message"]
    assert "secret" not in message
    assert "abc" not in message
    assert "https://<redacted>@host/path?token=<redacted>&x=<redacted>" in message
    # Surrounding non-URL text is untouched.
    assert "Client error '401 Unauthorized' for url" in message
    assert "- see docs" in message


def test_failure_message_redacts_raw_at_userinfo():
    text = "boom at 'https://u:p@ss@host/x?a=1' now"
    mcp_health.record_failure("Utilities", RuntimeError(text))

    message = mcp_health.snapshot("Utilities")["last_error"]["message"]
    assert "p@ss" not in message
    assert "https://<redacted>@host/x?a=<redacted>" in message


@pytest.mark.parametrize("hostile", ["a" * 200_000, "a." * 100_000])
def test_redact_scans_adversarial_scheme_run_linearly(hostile):
    # A long run of scheme-valid chars never followed by ``://`` must not backtrack
    # quadratically: the bounded scheme quantifier keeps the scan linear.
    start = time.monotonic()
    result = mcp_health._redact(hostile)
    elapsed = time.monotonic() - start

    assert result == hostile
    assert elapsed < 2.0


def test_failure_message_without_url_is_stored_unchanged():
    mcp_health.record_failure("Utilities", RuntimeError("connection reset by peer"))

    assert mcp_health.snapshot("Utilities")["last_error"]["message"] == "connection reset by peer"


def test_forget_drops_the_entry():
    mcp_health.record_failure("Utilities", RuntimeError("down"))
    assert "Utilities" in mcp_health._HEALTH

    mcp_health.forget("Utilities")

    assert "Utilities" not in mcp_health._HEALTH
    # A forgotten title reads back as never-called.
    assert mcp_health.snapshot("Utilities") == {
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
        "failing_since": None,
    }


def test_retain_keeps_listed_and_drops_unlisted():
    mcp_health.record_success("Utilities")
    mcp_health.record_failure("Events", RuntimeError("down"))
    mcp_health.record_success("Alerts")

    mcp_health.retain({"Utilities", "Alerts"})

    assert set(mcp_health._HEALTH) == {"Utilities", "Alerts"}
    # A surviving title keeps its history intact.
    assert mcp_health.snapshot("Utilities")["last_success"] is not None
    # The unlisted title reads back as never-called.
    assert mcp_health.snapshot("Events") == {
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
        "failing_since": None,
    }


def test_retain_empty_set_clears_all():
    mcp_health.record_success("Utilities")
    mcp_health.record_failure("Events", RuntimeError("down"))

    mcp_health.retain(set())

    assert mcp_health._HEALTH == {}


def test_retain_is_idempotent():
    mcp_health.record_success("Utilities")
    mcp_health.retain({"Utilities"})
    before = mcp_health.snapshot("Utilities")
    mcp_health.retain({"Utilities"})

    assert set(mcp_health._HEALTH) == {"Utilities"}
    assert mcp_health.snapshot("Utilities") == before


def test_forget_unknown_title_is_a_noop():
    # Idempotent cleanup: forgetting a title that never recorded is not an error.
    mcp_health.forget("never-recorded")
    assert "never-recorded" not in mcp_health._HEALTH


def test_concurrent_failures_are_counted_without_loss():
    per_thread = 200
    thread_count = 8
    barrier = threading.Barrier(thread_count)

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            mcp_health.record_failure("Utilities", RuntimeError("x"))

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert mcp_health.snapshot("Utilities")["consecutive_failures"] == thread_count * per_thread
