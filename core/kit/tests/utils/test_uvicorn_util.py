import pytest

from tai42_kit.utils.runtime.uvicorn_util import _is_value_token, parse_and_validate_uvicorn_args


class TestIsValueToken:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("--reload", False),  # double-dash flag
            ("--port=1", False),  # double-dash flag with inline value
            ("-q", False),  # single-dash flag
            ("-abc", False),  # single-dash flag, multi-char
            ("-", False),  # bare dash is not a value
            ("-5", True),  # negative int
            ("-5.5", True),  # negative float
            ("-5e3", True),  # negative scientific notation
            ("8000", True),  # plain number
            ("plain", True),  # plain word
            ("", True),  # empty token is a (degenerate) value
        ],
    )
    def test_table(self, token, expected):
        assert _is_value_token(token) is expected


class TestParseAndValidateUvicornArgs:
    def test_space_separated_pairs(self):
        out = parse_and_validate_uvicorn_args(("--host", "0.0.0.0", "--port", "8080"))
        assert out["host"] == "0.0.0.0"
        assert out["port"] == 8080
        assert isinstance(out["port"], int)

    def test_equals_form(self):
        out = parse_and_validate_uvicorn_args(("--port=9000",))
        assert out["port"] == 9000

    def test_dash_to_underscore_key(self):
        out = parse_and_validate_uvicorn_args(("--log-level", "debug"))
        assert out["log_level"] == "debug"

    def test_bool_true_variants(self):
        out = parse_and_validate_uvicorn_args(("--reload", "yes"))
        assert out["reload"] is True

    def test_bool_false_variants(self):
        out = parse_and_validate_uvicorn_args(("--reload", "off"))
        assert out["reload"] is False

    def test_flag_without_value_is_true(self):
        out = parse_and_validate_uvicorn_args(("--reload",))
        assert out["reload"] is True

    def test_negative_number_treated_as_value(self):
        # The parser recognizes -N as a value rather than the next flag.
        out = parse_and_validate_uvicorn_args(("--port", "-1"))
        assert out["port"] == -1

    def test_negative_float_treated_as_value(self):
        # A negative float after an unknown key is JSON-parsed as its value.
        out = parse_and_validate_uvicorn_args(("--custom-scale", "-5.5"))
        assert out["custom_scale"] == -5.5

    def test_single_dash_flag_not_consumed_as_value(self):
        # "-q" is a flag, not a value: the preceding flag stays bare-true and
        # parsing continues at the following tokens.
        out = parse_and_validate_uvicorn_args(("--reload", "-q", "--port", "8000"))
        assert out["reload"] is True
        assert out["port"] == 8000

    def test_unknown_key_json_parsed(self):
        out = parse_and_validate_uvicorn_args(("--custom-extra", '{"k": 1}'))
        assert out["custom_extra"] == {"k": 1}

    def test_unknown_key_non_json_kept_as_string(self):
        out = parse_and_validate_uvicorn_args(("--custom-extra", "plain"))
        assert out["custom_extra"] == "plain"

    def test_non_flag_tokens_ignored(self):
        out = parse_and_validate_uvicorn_args(("garbage", "--port", "8000"))
        assert out == {"port": 8000}

    def test_uncastable_int_falls_back_to_string(self):
        # "abc" cannot become int, so the original string is preserved.
        out = parse_and_validate_uvicorn_args(("--port", "abc"))
        assert out["port"] == "abc"

    def test_flag_followed_by_another_flag_is_true(self):
        # A known flag whose next token is itself a flag takes no value -> "true".
        out = parse_and_validate_uvicorn_args(("--reload", "--port", "8000"))
        assert out["reload"] is True
        assert out["port"] == 8000

    def test_json_value_wrong_type_falls_through_to_cast(self):
        # host is str-typed: json parses "123" to an int, which fails the type
        # check, so it is cast back to the string the str target wants.
        out = parse_and_validate_uvicorn_args(("--host", "123"))
        assert out["host"] == "123"

    def test_bool_unknown_token_raises(self):
        # A bool target with a token in neither true/false set must fail loudly
        # rather than silently casting every non-empty string to True.
        with pytest.raises(ValueError, match="not a recognized boolean token"):
            parse_and_validate_uvicorn_args(("--reload", "maybe"))

    def test_list_typed_arg_json_value_parsed(self):
        # reload_dirs is typed list[str] | str | None; a JSON array value is
        # loaded as a real list rather than passed through as a raw string.
        out = parse_and_validate_uvicorn_args(("--reload-dirs", '["src"]'))
        assert out["reload_dirs"] == ["src"]

    def test_list_typed_arg_bare_value_passed_raw_not_split(self):
        # A bare (non-JSON) value for a list-typed arg is passed through raw for
        # uvicorn's own validation — it must NOT be fed to list("src") and split
        # into characters.
        out = parse_and_validate_uvicorn_args(("--reload-dirs", "src"))
        assert out["reload_dirs"] == "src"
        assert out["reload_dirs"] != ["s", "r", "c"]
