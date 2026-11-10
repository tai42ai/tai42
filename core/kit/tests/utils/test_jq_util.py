"""The caller side of the jq seam: evaluation semantics, the compile check, the digest-first protocol."""

import pickle

import pytest

pytest.importorskip("jq")

from tai42_contract.errors import ErrorKind, error_kind, is_transport_error

from tai42_kit.compute.errors import ComputeWorkerLostError
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import jq_util, jq_worker
from tai42_kit.utils.data.jq_errors import (
    JqBudgetExceededError,
    JqCompileError,
    JqEvaluationError,
    JqNoOutputError,
    JqProgramError,
)
from tai42_kit.utils.data.jq_util import (
    _program_key,
    compile_check,
    run_jq_bounded,
    run_jq_first,
)


class _InProcessPool:
    """Runs each job in this process and records the job's name and arguments."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.raises = raises

    async def run(self, fn, /, *args, timeout=None):
        self.calls.append((fn.__name__, args))
        if self.raises is not None:
            raise self.raises
        return fn(*args)


@pytest.fixture
def in_process_pool(monkeypatch):
    pool = _InProcessPool()
    jq_worker._compiled.clear()
    monkeypatch.setattr(jq_util, "compute_pool", lambda: pool)
    yield pool
    jq_worker._compiled.clear()


class TestEnvGuard:
    # jq's ``env``/``$ENV`` read the whole process environment; the guard seals
    # both without breaking any legitimate ``.env``/``{env: …}`` usage.

    async def test_env_builtin_disabled_and_secret_absent(self, monkeypatch):
        monkeypatch.setenv("CANARY", "s3cr3t-should-never-surface")
        with pytest.raises(ValueError, match="env builtin is disabled") as exc:
            await run_jq_first("env.CANARY", {})
        assert "s3cr3t-should-never-surface" not in str(exc.value)

    async def test_env_variable_refused(self):
        with pytest.raises(ValueError, match=r"\$ENV is disabled"):
            await run_jq_first("$ENV.CANARY", {})

    async def test_user_env_definition_shadows_and_runs(self):
        assert await run_jq_first("def env: 1; env", {}) == 1

    async def test_dot_env_key_access_is_legitimate(self):
        assert await run_jq_first(".env", {"env": "x"}) == "x"

    async def test_object_with_env_key_is_legitimate(self):
        assert await run_jq_first("{env: .a}", {"a": 7}) == {"env": 7}

    async def test_user_function_is_legitimate(self):
        assert await run_jq_first("def f(x): x*2; f(.a)", {"a": 3}) == 6

    async def test_trailing_line_comment_is_accepted(self):
        assert await run_jq_first(".a\n# trailing comment", {"a": 11}) == 11


class TestEnvelopeSealed:
    async def test_the_refusal_reaches_run_jq_first(self):
        with pytest.raises(ValueError, match=r"\$__in is reserved"):
            await run_jq_first("$__in.v", {})

    async def test_the_refusal_reaches_compile_check(self):
        with pytest.raises(ValueError, match=r"\$__in is reserved"):
            await compile_check("$__in.d.outputs")

    async def test_a_longer_name_sharing_the_prefix_compiles(self):
        assert await run_jq_first(".a as $__inbox | $__inbox + 1", {"a": 1}) == 2


class TestRunJqFirst:
    async def test_returns_evaluated_value(self):
        assert await run_jq_first(".a", {"a": 42}) == 42

    async def test_empty_pipeline_without_default_raises_no_output(self):
        # An empty pipeline surfaces loudly, never as a silent ``None``.
        with pytest.raises(JqNoOutputError) as exc:
            await run_jq_first(".x[]", {"x": []})
        assert str(exc.value) == "jq expression produced no output (empty pipeline): '.x[]'"

    async def test_empty_pipeline_returns_default_when_supplied(self):
        # A supplied default substitutes for the empty pipeline; ``None`` and ``{}``
        # are both honoured (the sentinel distinguishes them from "no default").
        assert await run_jq_first(".[] | select(.x)", [], default=None) is None
        assert await run_jq_first(".x[]", {"x": []}, default=None) is None
        assert await run_jq_first(".[] | select(.x)", [], default={}) == {}

    async def test_non_empty_pipeline_returns_first_unchanged_with_default(self):
        # A default never shadows a real first value.
        assert await run_jq_first(".a", {"a": 42}, default=None) == 42

    async def test_a_syntax_error_raises_valueerror(self):
        with pytest.raises(ValueError, match="syntax error"):
            await run_jq_first("this is (not valid", {})


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
        # An expression emitting 100M values with limit 1 takes at most limit + 1 lazily.
        assert await run_jq_bounded("range(100000000)", None, limit=1) == [0, 1]

    async def test_binds_named_variables(self):
        result = await run_jq_bounded(".[] | . + $bump", [1, 2, 3], limit=5, variables={"bump": 10})
        assert result == [11, 12, 13]


class TestNamedVariables:
    async def test_run_jq_first_binds_named_variables(self):
        assert await run_jq_first(".a + $extra.b", {"a": 1}, variables={"extra": {"b": 4}}) == 5

    async def test_run_jq_first_without_the_variable_fails_loudly(self):
        # jq resolves a variable reference at compile time and refuses an undeclared one loudly.
        with pytest.raises(ValueError, match=r"\$extra is not defined"):
            await run_jq_first(".a + $extra.b", {"a": 1})

    async def test_prelude_def_reads_a_bound_variable_inside_map(self):
        result = await run_jq_first(
            "shift",
            [1, 2, 3],
            prelude="def shift: map(. + $bump);",
            variables={"bump": 10},
        )
        assert result == [11, 12, 13]

    async def test_a_reserved_variable_name_on_evaluation_is_a_compile_error(self):
        with pytest.raises(JqCompileError, match="reserved"):
            await run_jq_first(".", {}, variables={"__in": 1})


class TestCompileCheck:
    async def test_compile_check_declares_variables(self):
        await compile_check(".a <= $limit", variables=("limit",))
        with pytest.raises(ValueError, match=r"\$limit is not defined"):
            await compile_check(".a <= $limit")

    async def test_compile_check_still_refuses_a_syntax_error(self):
        with pytest.raises(ValueError, match="syntax error"):
            await compile_check(".a <=", variables=("limit",))

    async def test_compile_check_resolves_prelude_defs(self):
        await compile_check("double(.a)", prelude="def double($x): $x * 2;")
        with pytest.raises(ValueError, match="double/1 is not defined"):
            await compile_check("double(.a)")

    @pytest.mark.parametrize("name", ["__in", "ENV", "__loc__"])
    async def test_reserved_variable_name_is_refused(self, name):
        with pytest.raises(ValueError, match="reserved"):
            await compile_check(".", variables=(name,))

    async def test_a_compile_past_the_budget_names_it(self, monkeypatch):
        monkeypatch.setattr(jq_util, "compute_pool", lambda: _InProcessPool(raises=TimeoutError("job killed")))
        monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.5")
        reset_all_settings()
        try:
            with pytest.raises(JqBudgetExceededError) as exc:
                await compile_check(".a")
            assert str(exc.value) == "jq compile exceeded 0.5s (JQ_TIMEOUT_SECONDS); its worker was killed"
        finally:
            reset_all_settings()


class TestPreludePassthrough:
    async def test_run_jq_first_passes_prelude(self):
        assert await run_jq_first("double(.a)", {"a": 3}, prelude="def double($x): $x * 2;") == 6

    async def test_run_jq_bounded_passes_prelude(self):
        result = await run_jq_bounded(".[] | double(.)", [1, 2, 3], limit=5, prelude="def double($x): $x * 2;")
        assert result == [2, 4, 6]

    async def test_empty_prelude_is_unchanged_behavior(self):
        assert await run_jq_first(".a", {"a": 42}, prelude="") == 42


class TestDigestFirstProtocol:
    """A worker is sent the program's digest first; the source only when it reports a miss."""

    async def test_a_cold_evaluation_sends_the_source_after_the_miss(self, in_process_pool):
        assert await run_jq_first(".a", {"a": 1}) == 1
        assert [name for name, _ in in_process_pool.calls] == ["evaluate_cached_job", "compile_and_evaluate_job"]
        assert isinstance(in_process_pool.calls[1][1][1], jq_worker.JqSource)

    async def test_a_warm_evaluation_sends_the_digest_and_the_input_only(self, in_process_pool):
        await run_jq_first(".a + $bump", {"a": 1}, prelude="def f: 1;", variables={"bump": 10})
        in_process_pool.calls.clear()
        assert await run_jq_first(".a + $bump", {"a": 5}, prelude="def f: 1;", variables={"bump": 100}) == 105
        [(name, args)] = in_process_pool.calls
        assert name == "evaluate_cached_job"
        assert not any(isinstance(arg, jq_worker.JqSource) for arg in args)
        assert args[0] == _program_key(".a + $bump", "def f: 1;", ("bump",))

    async def test_a_compile_check_leaves_the_program_warm_for_its_evaluation(self, in_process_pool):
        await compile_check(".a + $n", variables=("n",))
        in_process_pool.calls.clear()
        assert await run_jq_first(".a + $n", {"a": 1}, variables={"n": 2}) == 3
        assert [name for name, _ in in_process_pool.calls] == ["evaluate_cached_job"]

    async def test_an_evaluation_past_the_budget_names_it(self, monkeypatch):
        monkeypatch.setattr(jq_util, "compute_pool", lambda: _InProcessPool(raises=TimeoutError("job killed")))
        with pytest.raises(JqBudgetExceededError, match=r"jq evaluation exceeded .*s \(JQ_TIMEOUT_SECONDS\)"):
            await run_jq_first(".a", {})

    def test_the_digest_separates_every_part(self):
        # Length-prefixed parts: moving characters across a part boundary changes the digest.
        assert _program_key("ab", "c", ()) != _program_key("a", "bc", ())
        assert _program_key(".a", "", ("x", "y")) != _program_key(".a", "", ("xy",))
        assert _program_key(".a", "", ()) != _program_key(".a", "", ("",))
        assert _program_key(".a", "def f: 1;", ("n",)) == _program_key(".a", "def f: 1;", ("n",))


class TestCompileErrorType:
    """A compile failure raises :class:`JqCompileError`; an evaluation failure :class:`JqEvaluationError`."""

    @pytest.mark.parametrize(
        ("expression", "prelude", "message"),
        [
            ("this is (not valid", "", "syntax error"),
            (".a + $extra.b", "", r"\$extra is not defined"),
            ("$ENV.CANARY", "", r"\$ENV is disabled"),
            ("$__in.v", "", r"\$__in is reserved"),
            (".a", "def broken: (;", "syntax error"),
        ],
    )
    async def test_a_compile_failure_from_run_jq_first_is_typed(self, expression, prelude, message):
        with pytest.raises(JqCompileError, match=message):
            await run_jq_first(expression, {"a": 1}, prelude=prelude)

    async def test_a_compile_failure_from_run_jq_bounded_is_typed(self):
        with pytest.raises(JqCompileError, match=r"\$extra is not defined"):
            await run_jq_bounded(".a + $extra.b", {"a": 1}, limit=1)

    async def test_a_compile_failure_from_compile_check_is_typed(self):
        with pytest.raises(JqCompileError, match="syntax error"):
            await compile_check(".a <=")

    async def test_an_evaluation_failure_is_an_evaluation_error(self):
        with pytest.raises(JqEvaluationError, match="cannot be added"):
            await run_jq_first(".a + 1", {"a": "x"})

    async def test_a_bounded_evaluation_failure_is_an_evaluation_error(self):
        with pytest.raises(JqEvaluationError, match="cannot be added"):
            await run_jq_bounded(".[] | . + 1", ["x"], limit=1)

    async def test_a_lone_surrogate_reaches_the_caller_as_a_compile_error(self):
        with pytest.raises(JqCompileError, match="not valid Unicode text at offset 1: surrogates not allowed"):
            await compile_check('"\ud800"')

    @pytest.mark.parametrize("error_type", [JqCompileError, JqEvaluationError, JqBudgetExceededError, JqNoOutputError])
    def test_each_type_survives_the_pickle_round_trip(self, error_type):
        restored = pickle.loads(pickle.dumps(error_type("jq: the message")))  # noqa: S301 -- the test's own payload
        assert type(restored) is error_type
        assert str(restored) == "jq: the message"


class TestProgramErrorClassification:
    """The author's program failing is bad input, never a transport failure; a pool fault passes through as itself."""

    async def test_a_budget_overrun_is_bad_input_and_not_transport(self, monkeypatch):
        monkeypatch.setattr(jq_util, "compute_pool", lambda: _InProcessPool(raises=TimeoutError("job killed")))
        with pytest.raises(JqBudgetExceededError) as exc:
            await run_jq_first(".a", {})
        assert isinstance(exc.value.__cause__, TimeoutError)
        assert error_kind(exc.value) is ErrorKind.BAD_INPUT
        assert is_transport_error(exc.value) is False

    @pytest.mark.parametrize("error_type", [JqCompileError, JqEvaluationError, JqBudgetExceededError, JqNoOutputError])
    def test_every_member_is_a_program_error_of_kind_bad_input(self, error_type):
        exc = error_type("x")
        assert isinstance(exc, JqProgramError)
        assert error_kind(exc) is ErrorKind.BAD_INPUT
        assert is_transport_error(exc) is False

    @pytest.mark.parametrize(
        "call",
        [
            lambda: run_jq_first(".a", {}),
            lambda: run_jq_bounded(".a", {}, 1),
            lambda: compile_check(".a"),
        ],
        ids=["run_jq_first", "run_jq_bounded", "compile_check"],
    )
    async def test_a_lost_worker_leaves_the_seam_as_itself(self, monkeypatch, call):
        lost = ComputeWorkerLostError("the worker running compute job j ended with exit code -9")
        monkeypatch.setattr(jq_util, "compute_pool", lambda: _InProcessPool(raises=lost))
        with pytest.raises(ComputeWorkerLostError) as exc:
            await call()
        assert exc.value is lost
