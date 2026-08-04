import hashlib
import keyword

from tai42_kit.utils.data.string_util import (
    hash_api_key,
    makefun_func_name,
    snake_to_pascal,
    text_to_md5,
)


class TestHashApiKey:
    def test_known_value(self):
        # sha256("sk-abc") is a stable, well-known digest.
        assert hash_api_key("sk-abc") == "1460db1b6902f8b1fc2a40d9381a24d0fd22c3bc1b2c6f999c521da73776fbe0"

    def test_deterministic(self):
        assert hash_api_key("sk-repeat") == hash_api_key("sk-repeat")

    def test_matches_hashlib_sha256(self):
        assert hash_api_key("sk-payload") == hashlib.sha256(b"sk-payload").hexdigest()

    def test_hex_length_is_64(self):
        assert len(hash_api_key("sk-anything")) == 64

    def test_distinct_inputs_distinct_hashes(self):
        assert hash_api_key("sk-abc") != hash_api_key("sk-xyz")


class TestTextToMd5:
    def test_known_value(self):
        # md5("hello") is a stable, well-known digest.
        assert text_to_md5("hello") == "5d41402abc4b2a76b9719d911017c592"

    def test_deterministic(self):
        assert text_to_md5("repeat") == text_to_md5("repeat")

    def test_matches_hashlib(self):
        assert text_to_md5("payload") == hashlib.md5(b"payload").hexdigest()

    def test_distinct_inputs_distinct_hashes(self):
        assert text_to_md5("a") != text_to_md5("b")

    def test_empty_string(self):
        assert text_to_md5("") == "d41d8cd98f00b204e9800998ecf8427e"


class TestMakefunFuncName:
    def test_plain_identifier_unchanged(self):
        assert makefun_func_name("lookup_tool") == "lookup_tool"

    def test_hyphens_become_underscores(self):
        assert makefun_func_name("look-up-tool") == "look_up_tool"

    def test_leading_digit_is_prefixed(self):
        assert makefun_func_name("2fa") == "_2fa"

    def test_python_keyword_is_prefixed(self):
        # ``class`` is a valid identifier per ``str.isidentifier`` but a keyword,
        # so ``def class(...)`` would not compile — prefix it.
        assert makefun_func_name("class") == "_class"

    def test_hyphen_leading_digit_and_keyword_segment_yields_identifier(self):
        result = makefun_func_name("2-lookup-class")
        assert result.isidentifier()
        assert not keyword.iskeyword(result)

    def test_output_is_always_a_usable_identifier(self):
        for name in ["", "-", "for", "3-2-1", "a b", "tool!", "async", "café", "a/b:c"]:
            result = makefun_func_name(name)
            assert result.isidentifier()
            assert not keyword.iskeyword(result)

    def test_internal_invalid_chars_substituted_in_place(self):
        # Every non-``[0-9A-Za-z_]`` char maps to ``_`` in place, not collapsed to
        # a lossy constant.
        assert makefun_func_name("a b") == "a_b"
        assert makefun_func_name("tool!") == "tool_"
        assert makefun_func_name("a/b:c") == "a_b_c"

    def test_distinct_invalid_inputs_stay_distinct(self):
        # Names carrying invalid chars but differing in their VALID chars stay
        # distinct valid identifiers (the old fallback collapsed all to one
        # constant). Two names differing ONLY by which invalid char they carry
        # (both mapped to ``_``) may still coincide — that is inherent.
        names = ["a!b", "x!y", "café", "naïve", "tool#1", "tool#2"]
        results = [makefun_func_name(n) for n in names]
        assert len(set(results)) == len(names)


class TestSnakeToPascal:
    def test_underscore_words_titlecased(self):
        assert snake_to_pascal("hello_world") == "HelloWorld"

    def test_single_word(self):
        assert snake_to_pascal("single") == "Single"

    def test_already_capitalized_lowered_after_first(self):
        # snake_to_pascal() lowercases the tail of each word.
        assert snake_to_pascal("ABC_DEF") == "AbcDef"

    def test_empty_string(self):
        assert snake_to_pascal("") == ""
