"""The run face: a fresh token per driver continuation bind and per nested run, kept across a routing bind."""

from __future__ import annotations

from tai42_contract.interactions import (
    ChainedResume,
    chained_lineage,
    current_run_face,
    get_chained_resume,
    get_resume_continuation_tool,
    open_nested_run_face,
    reset_chained_resume,
    reset_resume_continuation_tool,
    set_chained_resume,
    set_resume_continuation_tool,
)


def _routing(key: str) -> ChainedResume:
    return ChainedResume(delivery_tool="deliver_chained_park", chain_key=key, asked_by=("main",))


def test_every_continuation_bind_mints_a_fresh_face_and_the_reset_restores_it():
    root = current_run_face()
    first = set_resume_continuation_tool("driver_a")
    try:
        face_a = current_run_face()
        assert face_a is not root
        second = set_resume_continuation_tool("driver_a")
        try:
            assert current_run_face() is not face_a
        finally:
            reset_resume_continuation_tool(second)
        assert current_run_face() is face_a
    finally:
        reset_resume_continuation_tool(first)
    assert current_run_face() is root


def test_a_routing_bind_keeps_the_face():
    token = set_resume_continuation_tool("driver_a")
    try:
        face = current_run_face()
        routing_token = set_chained_resume(_routing("tai42:chained-park:k1"))
        try:
            assert current_run_face() is face
        finally:
            reset_chained_resume(routing_token)
        none_token = set_chained_resume(None)
        try:
            assert current_run_face() is face
        finally:
            reset_chained_resume(none_token)
    finally:
        reset_resume_continuation_tool(token)


def test_a_nested_run_face_is_fresh_and_keeps_the_continuation_routing_and_lineage():
    token = set_resume_continuation_tool("driver_a")
    try:
        routing_token = set_chained_resume(_routing("tai42:chained-park:k1"))
        inner = set_resume_continuation_tool("driver_b")
        try:
            face = current_run_face()
            lineage = chained_lineage()
            assert lineage == ("tai42:chained-park:k1",)
            nested = open_nested_run_face()
            try:
                assert current_run_face() is not face
                assert chained_lineage() == lineage
                assert get_resume_continuation_tool() == "driver_b"
                assert get_chained_resume() == _routing("tai42:chained-park:k1")
            finally:
                reset_resume_continuation_tool(nested)
            assert current_run_face() is face
        finally:
            reset_resume_continuation_tool(inner)
            reset_chained_resume(routing_token)
    finally:
        reset_resume_continuation_tool(token)


def test_each_nested_run_face_is_distinct():
    first = open_nested_run_face()
    try:
        face_one = current_run_face()
    finally:
        reset_resume_continuation_tool(first)
    second = open_nested_run_face()
    try:
        assert current_run_face() is not face_one
    finally:
        reset_resume_continuation_tool(second)
