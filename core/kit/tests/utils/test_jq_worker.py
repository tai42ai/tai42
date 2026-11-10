"""The worker side of the jq seam, run in-process: program shape, the compiled-program cache, the jobs."""

import json

import pytest

pytest.importorskip("jq")

from tai42_kit.utils.data import jq_worker
from tai42_kit.utils.data.jq_errors import JqCompileError, JqEvaluationError
from tai42_kit.utils.data.jq_worker import (
    JqSource,
    compile_and_evaluate_job,
    compile_job,
    compile_jq,
    envelope,
    evaluate_cached_job,
)


def _compile(expression: str, prelude: str = "", variables: tuple[str, ...] = ()):
    return compile_jq(JqSource(expression, prelude, variables))


def _first(program, data, variables=None):
    """Evaluate a compiled program over the ``{"v", "d"}`` envelope and take ``.first()``."""
    return program.input(envelope(data, variables)).first()


def _all(program, data, variables=None):
    """Evaluate a compiled program over the ``{"v", "d"}`` envelope and take ``.all()``."""
    return program.input(envelope(data, variables)).all()


def _text(data, variables=None) -> str:
    return json.dumps(envelope(data, variables))


@pytest.fixture(autouse=True)
def _empty_cache():
    jq_worker._compiled.clear()
    yield
    jq_worker._compiled.clear()


@pytest.fixture
def compiles(monkeypatch):
    """Every program text handed to ``jq.compile``, in order."""
    import jq

    seen: list[str] = []
    real = jq.compile

    def _counting(source, *args, **kwargs):
        seen.append(source)
        return real(source, *args, **kwargs)

    monkeypatch.setattr(jq, "compile", _counting)
    return seen


class TestCompileJq:
    def test_compiles_and_evaluates(self):
        assert _first(_compile(".a"), {"a": 42}) == 42

    def test_array_iteration(self):
        assert _all(_compile(".[] | .n"), [{"n": 1}, {"n": 2}]) == [1, 2]

    def test_invalid_expression_raises(self):
        with pytest.raises(ValueError, match="syntax error"):
            _compile("this is (not valid")

    def test_binds_named_variables(self):
        assert _first(_compile(".a + $extra.b", variables=("extra",)), {"a": 1}, {"extra": {"b": 4}}) == 5

    def test_an_undeclared_variable_is_refused(self):
        with pytest.raises(ValueError, match=r"\$limit is not defined"):
            _compile(".a <= $limit")

    @pytest.mark.parametrize("name", ["__in", "ENV", "__loc__"])
    def test_a_reserved_variable_name_is_refused(self, name):
        with pytest.raises(ValueError, match="reserved"):
            _compile(".", variables=(name,))

    def test_the_paren_escape_is_refused(self):
        for prelude in ("", "def a: 1;"):
            with pytest.raises(ValueError, match="syntax error"):
                _compile(".a) | (.b", prelude)


class TestEnvGuard:
    # jq's ``env``/``$ENV`` read the whole process environment; the guard seals
    # both without breaking any legitimate ``.env``/``{env: …}`` usage.

    def test_env_variable_rejected_at_compile(self):
        for expr in ("$ENV.CANARY", '"\\($ENV.CANARY)"', ". as $ENV | ."):
            with pytest.raises(ValueError, match=r"\$ENV is disabled"):
                _compile(expr)

    def test_user_def_referencing_env_variable_rejected(self):
        with pytest.raises(ValueError, match=r"\$ENV is disabled"):
            _compile("def env: $ENV; env")

    def test_env_variable_in_prelude_rejected(self):
        with pytest.raises(ValueError, match=r"\$ENV is disabled"):
            _compile(".x", prelude="def leak: $ENV;")


class TestEnvelopeSealed:
    # ``$__in`` is the binding mechanism's envelope; a program or a prelude def that
    # names it is refused before the compile, so channel data reaches a program
    # only as ``.`` and variables only as their own ``$name``.

    @pytest.mark.parametrize("expr", ["$__in.d", '"\\($__in.d)"', ". as $__in | .", "$__in"])
    def test_an_expression_naming_the_envelope_is_refused(self, expr):
        with pytest.raises(ValueError, match=r"\$__in is reserved"):
            _compile(expr)

    def test_a_prelude_def_naming_the_envelope_is_refused(self):
        with pytest.raises(ValueError, match=r"\$__in is reserved"):
            _compile("f", prelude="def f: $__in.d;")


class TestPrelude:
    # The prelude is a run of ``def …;`` declarations compiled ahead of the
    # expression so the expression may call them.

    def test_prelude_def_callable_from_expression(self):
        assert _first(_compile("double(.a)", prelude="def double($x): $x * 2;"), {"a": 21}) == 42

    def test_prelude_def_calling_a_sibling_def(self):
        # A prelude def may call an earlier sibling def by its bare name.
        assert _first(_compile(".x + two", prelude="def one: 1;\ndef two: one + one;"), {"x": 40}) == 42

    def test_prelude_def_placed_after_guard_still_evaluates(self):
        # The prelude lands after the guard's ``{} as $ENV | `` and the variable
        # bindings; a def there must still resolve and evaluate.
        assert _first(_compile("greet", prelude='def greet: "hi";'), None) == "hi"

    def test_prelude_def_reads_a_bound_variable_inside_map(self):
        # A prelude def, defined after the variable bindings, reads a bound variable
        # even inside ``map(...)`` where ``.`` is rebound to each item.
        program = _compile("shift", prelude="def shift: map(. + $bump);", variables=("bump",))
        assert _first(program, [1, 2, 3], {"bump": 10}) == [11, 12, 13]

    def test_author_line_number_preserved_despite_prelude(self):
        # An expression whose only syntax error sits on its own line 3 reports
        # line 3 — the prelude's lines are subtracted back out of the message.
        prelude = "def a: 1;\ndef b: 2;\ndef c: 3;\ndef d: 4;"
        expr = ".a |\n.b |\n.c d"
        with pytest.raises(ValueError, match="syntax error") as bare:
            _compile(expr)
        assert "line 3" in str(bare.value)
        with pytest.raises(ValueError, match="syntax error") as shifted:
            _compile(expr, prelude=prelude)
        assert "line 3" in str(shifted.value)
        assert "line 7" not in str(shifted.value)

    def test_prelude_without_trailing_newline_keeps_line_arithmetic(self):
        # A prelude that does not end in a newline is normalised so the
        # expression's first line is still line 1 for error reporting.
        with pytest.raises(ValueError, match="syntax error") as exc:
            _compile(".a\n.b c", prelude="def a: 1;\ndef b: 2;")
        assert "line 2" in str(exc.value)


class TestOneCompilePerProgram:
    """A program compiles once, guarded, whether it compiles or not."""

    def test_a_successful_compile_compiles_once(self, compiles):
        _compile(".a + $n", "def f: 1;", ("n",))
        assert len(compiles) == 1
        assert compiles[0].startswith("def env:")

    def test_a_failed_compile_compiles_once(self, compiles):
        with pytest.raises(JqCompileError, match="jq: 1 compile error"):
            _compile(".a + $nope", "def f: 1;", ("n",))
        assert len(compiles) == 1


class TestCompileErrorMessage:
    """A compile error names the author's own line and column and quotes only the author's text."""

    @pytest.mark.parametrize(
        ("expression", "prelude", "variables", "message"),
        [
            (
                "this is (not valid",
                "",
                (),
                "jq: error: syntax error, unexpected IDENT, expecting end of file at <top-level>, line 1, column 6:\n"
                "    this is (not valid\n"
                "         ^^\n"
                "jq: 1 compile error",
            ),
            (
                ". |",
                "",
                (),
                "jq: error: syntax error, unexpected end of file at <top-level>, line 1, column 3:\n"
                "    . |\n"
                "      ^\n"
                "jq: 1 compile error",
            ),
            (
                ".a |\n.b |\n.c d",
                "def a: 1;\ndef b: 2;\ndef c: 3;\ndef d: 4;",
                (),
                "jq: error: syntax error, unexpected IDENT, expecting end of file at <top-level>, line 3, column 4:\n"
                "    .c d\n"
                "       ^\n"
                "jq: 1 compile error",
            ),
            (
                ".a + $nope",
                "",
                (),
                "jq: error: $nope is not defined at <top-level>, line 1, column 6:\n"
                "    .a + $nope\n"
                "         ^^^^^\n"
                "jq: 1 compile error",
            ),
            (
                ".a + $nope",
                "def a: 1;",
                ("x",),
                "jq: error: $nope is not defined at <top-level>, line 1, column 6:\n"
                "    .a + $nope\n"
                "         ^^^^^\n"
                "jq: 1 compile error",
            ),
        ],
    )
    def test_an_expression_error_names_the_expression_line_and_column(self, expression, prelude, variables, message):
        with pytest.raises(ValueError, match="jq: 1 compile error") as exc:
            _compile(expression, prelude, variables)
        assert str(exc.value) == message

    @pytest.mark.parametrize(
        ("prelude", "message"),
        [
            (
                "def f: ;",
                "jq: error: syntax error, unexpected ';' at <prelude>, line 1, column 8:\n"
                "    def f: ;\n"
                "           ^\n"
                "jq: 1 compile error",
            ),
            (
                "def g: 1;\ndef f: ;",
                "jq: error: syntax error, unexpected ';' at <prelude>, line 2, column 8:\n"
                "    def f: ;\n"
                "           ^\n"
                "jq: 1 compile error",
            ),
        ],
    )
    def test_a_prelude_error_names_the_prelude_line_and_column(self, prelude, message):
        with pytest.raises(ValueError, match="jq: 1 compile error") as exc:
            _compile(".", prelude, ("x",))
        assert str(exc.value) == message

    @pytest.mark.parametrize("prelude", ["", "def f: 1;"])
    @pytest.mark.parametrize("expression", ["", "   ", "\n\t"])
    def test_an_empty_expression_is_refused(self, expression, prelude):
        with pytest.raises(JqCompileError) as exc:
            _compile(expression, prelude, ("x",))
        assert str(exc.value) == 'jq: the expression is empty (try ".")'

    def test_a_directive_is_jqs_own_syntax_error_on_the_authors_line(self):
        with pytest.raises(JqCompileError) as exc:
            _compile("module {a:1}; .")
        assert str(exc.value) == (
            "jq: error: syntax error, unexpected module at <top-level>, line 1, column 1:\n"
            "    module {a:1}; .\n"
            "    ^^^^^^\n"
            "jq: 1 compile error"
        )

    @pytest.mark.parametrize(
        ("expression", "token"),
        [('include "nope"; .', "include"), ('import "nope" as n; .', "import")],
    )
    def test_an_include_or_import_directive_is_refused_by_jqs_grammar(self, expression, token):
        with pytest.raises(JqCompileError, match=f"unexpected {token} at <top-level>, line 1, column 1:"):
            _compile(expression)

    def test_a_location_quoted_in_the_authors_text_is_kept_verbatim(self):
        expression = '"at <top-level>, line 1, column 5:" | $nope'
        with pytest.raises(JqCompileError) as exc:
            _compile(expression, "def f: 1;")
        assert str(exc.value) == (
            "jq: error: $nope is not defined at <top-level>, line 1, column 39:\n"
            f"    {expression}\n"
            f"    {' ' * 38}^^^^^\n"
            "jq: 1 compile error"
        )

    def test_an_error_after_a_two_line_prelude_names_the_expression_line(self):
        with pytest.raises(JqCompileError) as exc:
            _compile(".a |\n.b |\n.c |", "def f: 1;\ndef g: 2;")
        assert str(exc.value).startswith(
            "jq: error: syntax error, unexpected end of file at <top-level>, line 3, column 4:\n    .c |\n       ^"
        )

    def test_every_error_of_a_program_is_located(self):
        with pytest.raises(JqCompileError) as exc:
            _compile("$a | $b")
        message = str(exc.value)
        assert "jq: error: $a is not defined at <top-level>, line 1, column 1:" in message
        assert "jq: error: $b is not defined at <top-level>, line 1, column 6:" in message
        assert message.endswith("jq: 2 compile errors")

    @pytest.mark.parametrize(("expression", "prelude"), [('1\x00 | error("x")', ""), (".", "def f: 1;\x00")])
    def test_a_nul_character_is_refused(self, expression, prelude):
        with pytest.raises(JqCompileError) as exc:
            _compile(expression, prelude)
        assert str(exc.value) == "jq: the program contains a NUL character (U+0000), which jq cannot compile"

    def test_a_lone_surrogate_is_refused_at_its_offset(self):
        with pytest.raises(JqCompileError) as exc:
            _compile('"\ud800"')
        assert str(exc.value) == "jq: the program is not valid Unicode text at offset 1: surrogates not allowed"

    def test_a_lone_surrogate_in_the_prelude_is_located_in_the_authors_text(self):
        with pytest.raises(JqCompileError) as exc:
            _compile(".", 'def f: "\udc00";')
        assert str(exc.value) == "jq: the program is not valid Unicode text at offset 8: surrogates not allowed"

    @pytest.mark.parametrize("name", ["a-b", "1st", "caf\u00e9", ""])
    def test_a_variable_name_that_is_not_a_jq_identifier_is_refused(self, name):
        with pytest.raises(JqCompileError, match="are not jq identifiers") as exc:
            _compile(".", "", (name,))
        assert "$__in" not in str(exc.value)

    def test_a_keyword_is_a_bindable_variable_name(self):
        assert _first(_compile("$if", "", ("if",)), None, {"if": 1}) == 1


class TestJobs:
    def test_an_evaluation_under_an_unknown_digest_reports_the_miss(self):
        assert evaluate_cached_job("k-unknown", _text({"a": 1}), 1) is None

    def test_a_miss_compiles_caches_and_evaluates(self, compiles):
        assert compile_and_evaluate_job("k", JqSource(".a", "", ()), _text({"a": 1}), 1) == [1]
        assert evaluate_cached_job("k", _text({"a": 2}), 1) == [2]
        assert len(compiles) == 1

    def test_same_names_different_values_reuse_one_compile(self, compiles):
        source = JqSource(".a + $bump", "", ("bump",))
        assert compile_and_evaluate_job("k", source, _text({"a": 1}, {"bump": 10}), 1) == [11]
        assert evaluate_cached_job("k", _text({"a": 5}, {"bump": 100}), 1) == [105]
        assert len(compiles) == 1

    def test_a_cached_program_is_not_recompiled_when_its_source_arrives_again(self, compiles):
        compile_job("k", JqSource(".hit", "", ()))
        compile_job("k", JqSource(".hit", "", ()))
        assert compile_and_evaluate_job("k", JqSource(".hit", "", ()), _text({"hit": 3}), 1) == [3]
        assert len(compiles) == 1

    def test_a_compile_job_raises_the_compile_error_and_caches_nothing(self):
        with pytest.raises(ValueError, match="syntax error"):
            compile_job("k", JqSource("this is (not valid", "", ()))
        assert evaluate_cached_job("k", _text(None), 1) is None

    def test_the_count_bounds_the_values_taken_lazily(self):
        # A 100M-value stream under count 2 yields two values without materializing the rest.
        assert compile_and_evaluate_job("k", JqSource("range(100000000)", "", ()), _text(None), 2) == [0, 1]

    def test_an_empty_stream_is_the_empty_list(self):
        assert compile_and_evaluate_job("k", JqSource(".[]", "", ()), _text([]), 1) == []

    def test_an_authored_error_is_an_evaluation_error_with_its_text(self):
        with pytest.raises(JqEvaluationError) as exc:
            compile_and_evaluate_job("k", JqSource('error("boom")', "", ()), _text(None), 1)
        assert str(exc.value) == "boom"

    def test_a_runtime_type_error_is_an_evaluation_error_in_jqs_words(self):
        compile_job("k", JqSource(".a", "", ()))
        with pytest.raises(JqEvaluationError) as exc:
            evaluate_cached_job("k", _text(1), 1)
        assert str(exc.value) == 'Cannot index number with string ("a")'

    def test_the_least_recently_used_program_leaves_a_full_cache(self, monkeypatch):
        monkeypatch.setattr(jq_worker, "COMPILED_CACHE_SIZE", 2)
        compile_job("one", JqSource("1", "", ()))
        compile_job("two", JqSource("2", "", ()))
        assert evaluate_cached_job("one", _text(None), 1) == [1]
        compile_job("three", JqSource("3", "", ()))
        assert evaluate_cached_job("two", _text(None), 1) is None
        assert evaluate_cached_job("one", _text(None), 1) == [1]
        assert evaluate_cached_job("three", _text(None), 1) == [3]
