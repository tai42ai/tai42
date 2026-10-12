"""The config-mode name law the server and the CLI both read."""

from __future__ import annotations

import pytest

from tai42_contract.config.manager import CONFIG_MODE_ENV, CONFIG_MODE_PATTERN, FILE_CONFIG_MODE


def test_the_mode_env_name_and_the_builtin_file_mode():
    assert CONFIG_MODE_ENV == "TAI_CONFIG_MODE"
    assert FILE_CONFIG_MODE == "file"
    assert CONFIG_MODE_PATTERN.fullmatch(FILE_CONFIG_MODE)


@pytest.mark.parametrize("mode", ["file", "redis", "a", "s3_v2", "x9"])
def test_a_mode_name_is_a_lowercase_identifier(mode: str):
    assert CONFIG_MODE_PATTERN.fullmatch(mode)


@pytest.mark.parametrize("mode", ["", "File", "9x", "_x", "a-b", "a b", "redis\n"])
def test_other_mode_names_are_refused(mode: str):
    assert CONFIG_MODE_PATTERN.fullmatch(mode) is None
