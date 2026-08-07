"""Isolate the whole suite from the developer machine's ambient settings env.

``TaiBaseSettings`` reads both process env vars and a ``.env`` in the current
working directory, and ``.env.example`` invites developers to create one with
``LLM_MODEL=`` / ``REDIS_URL=`` / ``PG_HOST=`` etc. Any such export or a populated
repo-root ``.env`` would flip the default-asserting settings tests (in
``tests/settings`` and ``tests/llm``, which assert the built-in defaults). This
autouse fixture gives every test a clean slate regardless of the host.
"""

import os
from pathlib import Path

import pytest

# Env-var prefixes every kit settings group reads. ``LLM_`` also covers
# ``LLM_PROVIDER_``; ``CONTEXT_`` covers ``CONTEXT_OVERFLOW_`` and
# ``CONTEXT_EDITING_``; ``SUMMARIZATION_`` covers ``SUMMARIZATION_MIDDLEWARE_``;
# ``PG_`` / ``REDIS_`` cover the connection-settings field names;
# ``MCP_CLIENT_`` covers the MCP connect/call timeouts; ``JQ_`` covers the jq
# evaluation budget; ``TAI_URL_GUARD_`` covers the SSRF guard policy. Keep in
# step with the ``env_prefix`` of every settings class under ``src/tai42_kit``.
_SETTINGS_ENV_PREFIXES = (
    "LLM_",
    "EMBEDDING_",
    "TRIMMING_MIDDLEWARE_",
    "CONTEXT_",
    "SUMMARIZATION_",
    "PG_",
    "REDIS_",
    "MCP_CLIENT_",
    "JQ_",
    "TAI_URL_GUARD_",
    # The shared connection namespace the Postgres/Redis settings fall back to;
    # an ambient ``TAI_DEFAULT_*`` would resolve a connection the default-asserting
    # tests expect to be unconfigured.
    "TAI_DEFAULT_",
)

# Exact env-var names that share no prefix with the groups above: the logging
# global, plus the five unprefixed fields of the registry-excluded
# connection-settings bases (``RedisConnectionSettings.decode_responses`` /
# ``socket_timeout`` / ``socket_connect_timeout`` / ``retry_on_timeout`` /
# ``retry_attempts``), which no prefix above would match.
_SETTINGS_ENV_NAMES = (
    "TAI_LOG_LEVEL",
    "DECODE_RESPONSES",
    "SOCKET_TIMEOUT",
    "SOCKET_CONNECT_TIMEOUT",
    "RETRY_ON_TIMEOUT",
    "RETRY_ATTEMPTS",
)


@pytest.fixture(autouse=True)
def _reset_client_epoch() -> object:
    """Restore the process-wide client epoch after any test that advances it.

    ``advance_client_epoch`` mutates one module-level counter shared by the pool
    maps, the registries, and the settings-cache stamp; without a restore, a test
    that advances it would leak a non-zero epoch into every later test.
    """
    from tai42_kit.clients import base as client_base

    saved = client_base._client_epoch
    yield
    client_base._client_epoch = saved


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear ambient kit settings env and run each test from a scratch CWD.

    Removes every env var a kit settings class reads, then ``chdir``s into an
    empty ``tmp_path`` so a ``.env`` in the repo root cannot leak in (settings
    read ``.env`` relative to the CWD). A test that needs a value sets it itself
    after this fixture has cleared the slate.
    """
    for name in list(os.environ):
        if name.startswith(_SETTINGS_ENV_PREFIXES) or name in _SETTINGS_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
