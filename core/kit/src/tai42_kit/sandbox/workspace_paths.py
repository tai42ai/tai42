"""A session's ``cwd`` or file path resolved against the workspace root it mounts at.

For a provider whose session workspace sits at a fixed POSIX path inside the session's
runtime, as a container's mount point.
"""

from __future__ import annotations

import posixpath

from tai42_contract.sandbox import SandboxError


def resolve_workspace_path(path: str, *, root: str) -> str:
    """Resolve a ``cwd`` / file ``path`` against the workspace ``root``.

    A relative value resolves under ``root`` with containment enforced (an escaping ``..``
    is a loud :class:`SandboxError`); an absolute value is returned unchanged as the
    caller's own responsibility (the contract path rule).
    """
    if path.startswith("/"):
        return path
    resolved = posixpath.normpath(posixpath.join(root, path))
    if resolved != root and not resolved.startswith(root + "/"):
        raise SandboxError(f"path {path!r} escapes the workspace root {root!r}")
    return resolved
