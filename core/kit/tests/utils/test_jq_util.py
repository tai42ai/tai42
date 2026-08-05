import time

import pytest

pytest.importorskip("jq")

from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import jq_util
from tai42_kit.utils.data.jq_util import get_compiled_jq, run_jq_bounded, run_jq_first


class TestGetCompiledJq:
    def test_compiles_and_evaluates(self):
        program = get_compiled_jq(".a")
        assert program.input(text='{"a": 42}').first() == 42

    def test_array_iteration(self):
        program = get_compiled_jq(".[] | .n")
        assert program.input(text='[{"n": 1}, {"n": 2}]').all() == [1, 2]

    def test_cached_returns_same_object(self):
        # lru_cache means the same expression yields the identical compiled object.
        assert get_compiled_jq(".cached.expr") is get_compiled_jq(".cached.expr")

    def test_invalid_expression_raises(self):
        with pytest.raises(ValueError, match="syntax error"):
            get_compiled_jq("this is (not valid")


class TestRunJqFirst:
    async def test_returns_evaluated_value(self):
        # Evaluates off-loop and returns .first() over a plain python payload.
        assert await run_jq_first(".a", {"a": 42}) == 42

    async def test_timeout_raises_named_promptly(self, monkeypatch):
        # A slow evaluation is bounded by JQ_TIMEOUT_SECONDS; the raised
        # TimeoutError names the env var and returns promptly (well under the
        # 1s the fake would otherwise block for).
        class _SlowProgram:
            def input(self, payload):
                return self

            def first(self):
                time.sleep(1)
                return None

        monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr: _SlowProgram())
        monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
        reset_all_settings()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
                await run_jq_first(".", {})
            assert time.monotonic() - start < 0.5
        finally:
            reset_all_settings()


class TestRunJqBounded:
    async def test_returns_up_to_limit_plus_one_values(self):
        # Three emits under limit 2 stop at limit + 1, so the caller reads len > limit.
        assert await run_jq_bounded(".[] | .n", [{"n": 1}, {"n": 2}, {"n": 3}], limit=2) == [1, 2, 3]

    async def test_single_emit_is_a_one_element_list(self):
        assert await run_jq_bounded("{x: .a}", {"a": 1}, limit=1) == [{"x": 1}]

    async def test_zero_emit_is_the_empty_list(self):
        assert await run_jq_bounded(".[]", [], limit=1) == []

    async def test_non_positive_limit_raises(self):
        with pytest.raises(ValueError, match="limit must be positive"):
            await run_jq_bounded(".", {}, limit=0)

    async def test_unbounded_stream_is_not_materialized(self):
        # An expression emitting 100M values with limit 1 must take at most limit + 1
        # lazily and return promptly — never allocating the whole stream.
        start = time.monotonic()
        result = await run_jq_bounded("range(100000000)", None, limit=1)
        assert len(result) <= 2
        assert time.monotonic() - start < 0.5

    async def test_timeout_raises_named_promptly(self, monkeypatch):
        # A slow evaluation is bounded by JQ_TIMEOUT_SECONDS; the raised TimeoutError
        # names the env var and returns promptly (well under the 1s the fake blocks for).
        class _SlowProgram:
            def input(self, payload):
                return self

            def __iter__(self):
                time.sleep(1)
                return iter([])

        monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr: _SlowProgram())
        monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
        reset_all_settings()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
                await run_jq_bounded(".", {}, limit=1)
            assert time.monotonic() - start < 0.5
        finally:
            reset_all_settings()
