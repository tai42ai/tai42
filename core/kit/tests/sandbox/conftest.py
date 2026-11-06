"""The ``ls`` the exec group scripts list open files with, for the modules that run them under a real shell."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# The ``ls`` on the host's PATH (coreutils on a Debian image), and busybox's applet, which a
# session image may carry instead: each lists an fd link it cannot read in its own way.
_LS_COMMANDS = ("ls-on-path", "busybox-ls")


@pytest.fixture(params=_LS_COMMANDS)
def ls_command(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Run the test with the scripts' ``ls`` the one the param names, put first on PATH for the processes it starts."""
    if request.param == "ls-on-path":
        return
    busybox = shutil.which("busybox")
    if busybox is None:
        pytest.skip("busybox is not on this host")
    bin_dir: Path = tmp_path_factory.mktemp("busybox-ls")
    (bin_dir / "ls").symlink_to(busybox)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
