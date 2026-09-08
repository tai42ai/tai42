"""Unit coverage for the shared command helpers in ``commands._common``.

The input parsers each own a usage-error contract the command wrappers rely on;
these exercise those contracts directly, with no server involved.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
import typer

from tai42_cli.commands import _common


def test_app_context_rejects_uninitialized_context() -> None:
    # The root callback stashes an ``AppContext`` on ``ctx.obj``; anything else means
    # the command ran without initialization, which must fail loudly.
    ctx = cast("typer.Context", SimpleNamespace(obj=object()))
    with pytest.raises(RuntimeError, match="not initialized"):
        _common.app_context(ctx)


def test_parse_json_object_rejects_invalid_json() -> None:
    with pytest.raises(typer.BadParameter, match="valid JSON"):
        _common.parse_json_object("{not json", param_hint="--x")


def test_parse_json_object_rejects_non_object() -> None:
    with pytest.raises(typer.BadParameter, match="JSON object"):
        _common.parse_json_object("[1, 2]", param_hint="--x")


def test_parse_json_object_accepts_object() -> None:
    assert _common.parse_json_object('{"a": 1}', param_hint="--x") == {"a": 1}


def test_parse_json_value_rejects_invalid_json() -> None:
    with pytest.raises(typer.BadParameter, match="valid JSON"):
        _common.parse_json_value("{not json", param_hint="--x")


def test_parse_json_value_accepts_any_json() -> None:
    assert _common.parse_json_value("[1, 2]", param_hint="--x") == [1, 2]


def test_parse_kwargs_merges_json_and_pairs_with_pair_override() -> None:
    # The base JSON object seeds the mapping; a ``--kw`` pair for the same key wins.
    result = _common.parse_kwargs('{"a": 1, "b": 2}', ["b=3", "c=hello"])
    # ``3`` parses as JSON (an int), while the unquoted ``hello`` falls back to the string.
    assert result == {"a": 1, "b": 3, "c": "hello"}


def test_parse_kwargs_rejects_pair_without_equals() -> None:
    with pytest.raises(typer.BadParameter, match="key=value"):
        _common.parse_kwargs(None, ["noequals"])


def test_echo_stderr_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    _common.echo_stderr("heads up")
    captured = capsys.readouterr()
    assert captured.err.strip() == "heads up"
    assert captured.out == ""


def _ctx(*, json_output: bool = False):
    return cast("_common.AppContext", SimpleNamespace(json_output=json_output))


def test_emit_records_derives_columns_and_items_key_from_the_route(capsys: pytest.CaptureFixture[str]) -> None:
    # GET /api/system/kinds -> SystemKindsListing (a bare list of rows): the derived
    # columns are the row model's fields, and the un-enveloped list is the data.
    data = [{"kind": "backend", "state": "active", "plugin": "pg", "detail": "ok"}]
    _common.emit_records(_ctx(), data, route=("GET", "/api/system/kinds"))
    out = capsys.readouterr().out
    header = out.splitlines()[0].split()
    assert header == ["kind", "state", "plugin", "detail"]
    assert "backend" in out


def test_emit_records_derives_the_envelope_items_key(capsys: pytest.CaptureFixture[str]) -> None:
    # GET /api/notifications -> NotificationListing{notifications: [...]}: the derived
    # items_key pulls the row list out of the envelope for the table.
    data = {"notifications": [{"id": "n1", "message": "hi", "recipient": "alice"}]}
    _common.emit_records(_ctx(), data, route=("GET", "/api/notifications"))
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[0] == "id"
    assert "n1" in out


def test_emit_records_wraps_a_bare_scalar_row_under_the_value_column(capsys: pytest.CaptureFixture[str]) -> None:
    # GET /api/tools -> StringListResponse (a bare list of names): each scalar row is
    # wrapped under the single derived ``value`` column.
    _common.emit_records(_ctx(), ["echo", "weather"], route=("GET", "/api/tools"))
    out = capsys.readouterr().out
    assert out.splitlines()[0].strip() == "value"
    assert "echo" in out


def test_emit_records_keeps_explicit_columns_for_a_non_derivable_route(capsys: pytest.CaptureFixture[str]) -> None:
    data = {"providers": [{"id": "p1", "display_name": "P1", "kind": "oauth", "category": "chat"}]}
    _common.emit_records(_ctx(), data, ["id", "display_name"], items_key="providers")
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["id", "display_name"]


def test_emit_records_under_json_emits_the_raw_payload(capsys: pytest.CaptureFixture[str]) -> None:
    _common.emit_records(_ctx(json_output=True), ["echo"], route=("GET", "/api/tools"))
    assert capsys.readouterr().out.strip() == '[\n  "echo"\n]'


def test_emit_records_rejects_both_a_route_and_explicit_columns() -> None:
    with pytest.raises(ValueError, match="one or the other"):
        _common.emit_records(_ctx(), [], ["x"], route=("GET", "/api/tools"))


def test_emit_records_requires_a_route_or_columns() -> None:
    with pytest.raises(ValueError, match=r"route .* or an explicit columns"):
        _common.emit_records(_ctx(), [])


def test_emit_records_raises_on_a_route_with_no_table_entry() -> None:
    with pytest.raises(KeyError, match="no derived shape"):
        _common.emit_records(_ctx(), [], route=("GET", "/api/does-not-exist"))
