"""Validator test for ``backend/callback.py`` — ``CallbackSchema.tool`` is optional."""

from __future__ import annotations

from tai42_contract.backend import CallbackSchema


def test_callback_schema_tool_defaults_empty():
    # No tool set: the backend runs the rendered expr directly.
    assert CallbackSchema().tool == ""
    assert CallbackSchema(tool="do_thing").tool == "do_thing"
