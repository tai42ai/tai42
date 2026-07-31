import hashlib

from tai42_kit.utils.data.string_util import hash_api_key, snake_to_pascal, text_to_md5


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
