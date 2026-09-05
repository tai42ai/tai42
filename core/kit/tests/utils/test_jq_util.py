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


class TestEnvGuard:
    # jq's ``env``/``$ENV`` read the whole process environment; the guard seals
    # both without breaking any legitimate ``.env``/``{env: …}`` usage.

    @pytest.fixture(autouse=True)
    def _canary(self, monkeypatch):
        monkeypatch.setenv("CANARY", "s3cr3t-should-never-surface")
        get_compiled_jq.cache_clear()
        yield
        get_compiled_jq.cache_clear()

    async def test_env_builtin_disabled_and_secret_absent(self):
        with pytest.raises(ValueError, match="env builtin is disabled") as exc:
            await run_jq_first("env.CANARY", {})
        assert "s3cr3t-should-never-surface" not in str(exc.value)

    def test_env_variable_rejected_at_compile(self):
        for expr in ("$ENV.CANARY", '"\\($ENV.CANARY)"', ". as $ENV | ."):
            with pytest.raises(ValueError, match=r"\$ENV is disabled"):
                get_compiled_jq(expr)

    async def test_user_env_definition_shadows_and_runs(self):
        assert await run_jq_first("def env: 1; env", {}) == 1

    def test_user_def_referencing_env_variable_rejected(self):
        with pytest.raises(ValueError, match=r"\$ENV is disabled"):
            get_compiled_jq("def env: $ENV; env")

    async def test_dot_env_key_access_is_legitimate(self):
        assert await run_jq_first(".env", {"env": "x"}) == "x"

    async def test_object_with_env_key_is_legitimate(self):
        assert await run_jq_first("{env: .a}", {"a": 7}) == {"env": 7}

    async def test_user_function_is_legitimate(self):
        assert await run_jq_first("def f(x): x*2; f(.a)", {"a": 3}) == 6

    async def test_trailing_comment_does_not_swallow_guard_paren(self):
        assert await run_jq_first(".a\n# trailing comment", {"a": 11}) == 11

    def test_syntax_error_reports_author_position(self):
        # The raw pre-compile runs first, so the reported error names the
        # author's own position, not an offset shifted by the preamble.
        with pytest.raises(ValueError, match="syntax error"):
            get_compiled_jq("this is (not valid")

    def test_empty_expression_still_raises(self):
        with pytest.raises(ValueError, match="compile error"):
            get_compiled_jq("")


class TestRunJqFirst:
    async def test_returns_evaluated_value(self):
        # Evaluates off-loop and returns .first() over a plain python payload.
        assert await run_jq_first(".a", {"a": 42}) == 42

    async def test_empty_pipeline_without_default_raises_valueerror(self):
        # An empty pipeline (``.first()`` raises ``StopIteration``) surfaces as a
        # loud ``ValueError`` — NOT the opaque ``RuntimeError: StopIteration
        # interacts badly with generators ...`` the raw future boundary produced.
        with pytest.raises(ValueError, match="empty pipeline"):
            await run_jq_first(".[] | select(.x)", [])

    async def test_empty_pipeline_returns_default_when_supplied(self):
        # A supplied default substitutes for the empty pipeline; ``None`` and ``{}``
        # are both honoured (the sentinel distinguishes them from "no default").
        assert await run_jq_first(".[] | select(.x)", [], default=None) is None
        assert await run_jq_first(".[] | select(.x)", [], default={}) == {}

    async def test_non_empty_pipeline_returns_first_unchanged_with_default(self):
        # A default never shadows a real first value.
        assert await run_jq_first(".a", {"a": 42}, default=None) == 42

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

        monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr, prelude="": _SlowProgram())
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

        monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr, prelude="": _SlowProgram())
        monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
        reset_all_settings()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
                await run_jq_bounded(".", {}, limit=1)
            assert time.monotonic() - start < 0.5
        finally:
            reset_all_settings()


class TestPrelude:
    # ``get_compiled_jq(expression, prelude)`` compiles a run of ``def …;``
    # declarations ahead of the expression so the expression may call them; the
    # cache key is the (expression, prelude) pair.

    def test_prelude_def_callable_from_expression(self):
        program = get_compiled_jq("double(.a)", prelude="def double($x): $x * 2;")
        assert program.input(text='{"a": 21}').first() == 42

    def test_prelude_def_calling_a_sibling_def(self):
        # A prelude def may call an earlier sibling def by its bare name — the
        # nested-library shape a views prelude emits.
        prelude = "def one: 1;\ndef two: one + one;"
        program = get_compiled_jq(".x + two", prelude=prelude)
        assert program.input(text='{"x": 40}').first() == 42

    def test_prelude_def_placed_after_guard_still_evaluates(self):
        # The prelude lands after the guard's ``{} as $ENV | (``; a def there must
        # still resolve and evaluate.
        program = get_compiled_jq("greet", prelude='def greet: "hi";')
        assert program.input(text="null").first() == "hi"

    def test_cache_keyed_on_the_pair(self):
        # Same expression, two preludes -> two distinct compiled programs.
        p_one = get_compiled_jq(".x", prelude="def f: 1;")
        p_two = get_compiled_jq(".x", prelude="def f: 2;")
        assert p_one is not p_two
        # An identical pair returns the identical cached object.
        assert get_compiled_jq(".x", prelude="def f: 1;") is p_one
        # The bare-expression cache and the pair cache never collide.
        assert get_compiled_jq(".x") is not p_one

    def test_env_variable_in_prelude_rejected(self):
        with pytest.raises(ValueError, match=r"\$ENV is disabled"):
            get_compiled_jq(".x", prelude="def leak: $ENV;")

    def test_author_line_number_preserved_despite_prelude(self):
        # An expression whose only syntax error sits on its own line 3 reports
        # line 3 — the prelude's lines are subtracted back out of the message.
        prelude = "def a: 1;\ndef b: 2;\ndef c: 3;\ndef d: 4;"
        expr = ".a |\n.b |\n.c d"
        with pytest.raises(ValueError, match="syntax error") as bare:
            get_compiled_jq(expr)
        assert "line 3" in str(bare.value)
        with pytest.raises(ValueError, match="syntax error") as shifted:
            get_compiled_jq(expr, prelude=prelude)
        assert "line 3" in str(shifted.value)
        assert "line 7" not in str(shifted.value)

    def test_prelude_without_trailing_newline_keeps_line_arithmetic(self):
        # A prelude that does not end in a newline is normalised so the
        # expression's first line is still line 1 for error reporting.
        prelude = "def a: 1;\ndef b: 2;"  # two lines, no trailing newline
        expr = ".a\n.b c"  # error on expr line 2
        with pytest.raises(ValueError, match="syntax error") as exc:
            get_compiled_jq(expr, prelude=prelude)
        assert "line 2" in str(exc.value)

    async def test_empty_prelude_is_unchanged_behavior(self):
        # The default empty prelude yields a program identical in behavior to a
        # bare call (the guard wrapper alone, no extra defs).
        assert await run_jq_first(".a", {"a": 42}, prelude="") == 42
        assert get_compiled_jq(".a", prelude="").input(text='{"a": 5}').first() == 5


class TestPreludePassthrough:
    async def test_run_jq_first_passes_prelude(self):
        assert await run_jq_first("double(.a)", {"a": 3}, prelude="def double($x): $x * 2;") == 6

    async def test_run_jq_bounded_passes_prelude(self):
        result = await run_jq_bounded(".[] | double(.)", [1, 2, 3], limit=5, prelude="def double($x): $x * 2;")
        assert result == [2, 4, 6]
