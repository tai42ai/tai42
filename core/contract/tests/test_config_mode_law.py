"""The config-mode name law the server and the CLI both read."""

from __future__ import annotations

import pytest

from tai42_contract.config.manager import (
    CONFIG_DIR_ENV,
    CONFIG_MODE_ENV,
    CONFIG_MODE_PATTERN,
    DEFAULT_CONFIG_DIR,
    FILE_CONFIG_MODE,
    FILE_STORED_ENV_NAME,
    file_config_dir,
    file_stored_env_path,
)


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


def test_the_file_provider_location_names():
    assert CONFIG_DIR_ENV == "TAI_CONFIG_DIR_PATH"
    assert DEFAULT_CONFIG_DIR == "/app"
    assert FILE_STORED_ENV_NAME == ".env"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"TAI_CONFIG_DIR_PATH": "/srv/conf"}, "/srv/conf"),
        ({"TAI_CONFIG_DIR_PATH": "  ./conf  "}, "./conf"),
        ({"TAI_CONFIG_DIR_PATH": "   "}, "/app"),
        ({}, "/app"),
    ],
)
def test_the_file_config_dir_is_the_set_dir_stripped_else_the_default(environ: dict[str, str], expected: str):
    assert file_config_dir(environ) == expected


def test_the_stored_env_is_the_dotenv_in_the_file_config_dir():
    assert file_stored_env_path({"TAI_CONFIG_DIR_PATH": "/srv/conf"}) == "/srv/conf/.env"
    assert file_stored_env_path({}) == "/app/.env"
