"""A session's workspace-relative path resolved against its workspace root."""

from __future__ import annotations

import pytest
from tai42_contract.sandbox import SandboxError

from tai42_kit.sandbox.workspace_paths import resolve_workspace_path


def test_a_relative_path_resolves_under_the_workspace_root() -> None:
    assert resolve_workspace_path("sub/./nested.txt", root="/workspace") == "/workspace/sub/nested.txt"
    assert resolve_workspace_path("sub/..", root="/workspace") == "/workspace"


def test_an_absolute_path_is_returned_as_given() -> None:
    assert resolve_workspace_path("/etc/passwd", root="/workspace") == "/etc/passwd"


@pytest.mark.parametrize("path", ["../etc/passwd", "sub/../../x", "..", "../workspace2/x"])
def test_a_relative_path_that_escapes_the_root_raises(path: str) -> None:
    with pytest.raises(SandboxError, match=r"^path '.*' escapes the workspace root '/workspace'$"):
        resolve_workspace_path(path, root="/workspace")
