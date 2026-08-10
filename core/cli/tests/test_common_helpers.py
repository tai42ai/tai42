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
