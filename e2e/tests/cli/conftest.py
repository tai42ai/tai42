"""B7 — the CLI-coverage stack fixture.

Boots the broad access-controlled stack (``_cli_support.build_cli_stack``) once,
seeded with a root key so the CLI authenticates over ``TAI_API_KEY``. The spec runs
the real ``tai`` console script against it via subprocess."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tai42_e2e.booting import boot_stack
from tai42_e2e.stack import Infra, TaiStack

from ._cli_support import build_cli_stack  # pyright: ignore[reportMissingImports]


@pytest.fixture(scope="module")
def cli_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    for stack in boot_stack(infra, tmp_path_factory.mktemp("cli"), build_cli_stack, seed_auth=True):
        assert stack.auth_token is not None, "cli_stack must boot with a seeded root token"
        yield stack
