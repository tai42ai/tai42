"""``tai fleet`` command-group projection + the ``--json`` split.

The ``workers`` command projects each raw API row into a fixed set of human display
columns (``_worker_display_row`` / ``_relative_since`` / ``_WORKER_COLUMNS``) for the
table, while ``--json`` emits the RAW API envelope UNPROJECTED. These tests pin that
split and the projection's edge rules: the ``(stale)`` suffix comes ONLY from the
server ``stale`` flag (never a client-side threshold), and a missing/unparseable stamp
renders ``—``.
"""

from __future__ import annotations

import json
from typing import Any

from tai42_cli.commands.fleet import (
    _WORKER_COLUMNS,
    _relative_since,
    _worker_display_row,
)

from .remote_harness import data_response, run_cli, strip_ansi


def _worker(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "serve-1",
        "kind": "serve",
        "pid": 1234,
        "generation": 2,
        "joined_at": "2026-08-08T00:00:00+00:00",
        "beat_at": "2026-08-08T00:00:00+00:00",
        "state": "ready",
        "stale": False,
        "last_op": None,
    }
    base.update(overrides)
    return base


# -- _worker_display_row projection ------------------------------------------


def test_display_row_projects_the_fixed_columns() -> None:
    row = _worker_display_row(
        _worker(pid=99, generation=7, state="ready", last_op={"op": "recycle", "outcome": "applied"})
    )
    assert set(_WORKER_COLUMNS) <= set(row)
    assert row["name"] == "serve-1"
    assert row["kind"] == "serve"
    assert row["pid"] == 99
    assert row["gen"] == 7
    assert row["last-op"] == "recycle:applied"


def test_state_suffix_comes_only_from_server_stale_flag() -> None:
    # The server flag drives the suffix.
    stale = _worker_display_row(_worker(state="ready", stale=True))
    assert stale["state"] == "ready (stale)"
    # A genuinely ancient beat but stale=False must NOT gain a suffix — the projection
    # has no client-side freshness threshold of its own.
    fresh_old = _worker_display_row(_worker(state="ready", stale=False, beat_at="2000-01-01T00:00:00+00:00"))
    assert fresh_old["state"] == "ready"
    assert "(stale)" not in fresh_old["state"]


def test_seen_since_and_last_op_render_dash_for_missing_or_unparseable() -> None:
    missing = _worker_display_row(_worker(beat_at=None, last_op=None))
    assert missing["seen-since"] == "—"
    assert missing["last-op"] == "—"
    unparseable = _worker_display_row(_worker(beat_at="not-a-timestamp"))
    assert unparseable["seen-since"] == "—"


def test_relative_since_dash_vs_ago() -> None:
    assert _relative_since(None) == "—"
    assert _relative_since("") == "—"
    assert _relative_since("not-a-timestamp") == "—"
    # A valid-but-naive stamp (no offset) parses but the aware-minus-naive subtract
    # would raise TypeError; the render degrades to a dash rather than crashing.
    assert _relative_since("2020-01-01T00:00:00") == "—"
    # A parseable stamp renders a coarse "<n><unit> ago", never a dash.
    rendered = _relative_since("2026-08-08T00:00:00+00:00")
    assert rendered != "—"
    assert rendered.endswith("ago")


# -- the workers command: human table vs --json ------------------------------

_ENVELOPE = {
    "workers": [
        {
            "name": "serve-1",
            "kind": "serve",
            "pid": 11,
            "generation": 3,
            "joined_at": "2026-08-08T00:00:00+00:00",
            "beat_at": "2026-08-08T00:00:00+00:00",
            "state": "ready",
            "stale": True,
            "last_op": {"op": "recycle", "outcome": "applied"},
        }
    ]
}


def test_workers_human_table_shows_columns_and_server_stale_suffix(monkeypatch) -> None:
    result = run_cli(monkeypatch, lambda request: data_response(_ENVELOPE), ["fleet", "workers"])
    assert result.exit_code == 0, result.output
    out = strip_ansi(result.output)
    for column in ("name", "kind", "pid", "gen", "state", "seen-since", "last-op"):
        assert column in out, f"missing column header {column!r}"
    # The server stale flag surfaces as the (stale) suffix; the projected last-op cell shows.
    assert "(stale)" in out
    assert "recycle:applied" in out


def test_workers_json_emits_raw_envelope_unprojected(monkeypatch) -> None:
    result = run_cli(monkeypatch, lambda request: data_response(_ENVELOPE), ["fleet", "workers"], json_output=True)
    assert result.exit_code == 0, result.output
    out = strip_ansi(result.output)
    payload = json.loads(out)
    # The RAW API envelope, byte-for-byte — NOT the projected display rows.
    assert payload == _ENVELOPE
    # None of the projected-only display keys leak into --json output.
    assert "seen-since" not in out
    assert "gen" not in {key for row in payload["workers"] for key in row}
    assert "(stale)" not in out
