"""Import-graph guard: the shipped package imports only allowlisted roots.

Two walks share one allowlist. The runtime walk imports the package in a fresh
subprocess and inspects ``sys.modules``, so it sees the shipped closure alone.
The static walk parses the AST of every source file, catching imports nested in
functions, class bodies, or ``TYPE_CHECKING`` blocks that never run on import.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

# The shipped package and the public first-party packages it may import.
PACKAGE = "tai42_storage_github"
ALLOWED_FIRST_PARTY = frozenset({PACKAGE, "tai42_contract", "tai42_kit"})

# Third-party roots the shipped graph pulls in: declared runtime deps plus their
# resolved closure. Compiled extensions appear under their registered top-level
# name (e.g. ``_cffi_backend`` from ``cffi``).
ALLOWED_THIRD_PARTY = frozenset(
    {
        "annotated_types",
        "click",
        "dotenv",
        "httpx",
        "idna",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "pygments",
        "rich",
        "typing_extensions",
        "typing_inspection",
    }
)

# Interpreter/compiler/virtual-env roots that land in ``sys.modules`` as ambient
# side effects. Build/platform/version specific, so matched by shape, not literal.
_ARTIFACT_ROOTS = frozenset({"__main__", "__mp_main__", "cython_runtime", "_virtualenv"})


def _is_runtime_artifact(root: str) -> bool:
    return root in _ARTIFACT_ROOTS or root.endswith("__mypyc") or root.startswith(("_cython_", "_sysconfigdata"))


def _allowed(root: str) -> bool:
    return (
        root in sys.stdlib_module_names
        or root in ALLOWED_FIRST_PARTY
        or root in ALLOWED_THIRD_PARTY
        or _is_runtime_artifact(root)
    )


# Program run in the subprocess: bind a stub app, import the package and every
# submodule, then print each imported root not on the allowlist. A submodule that
# fails to import exits non-zero, which the parent turns into a loud failure.
_CHILD_PROGRAM = f"""
import importlib
import pkgutil
import sys

from tai42_contract.app import tai42_app

PACKAGE = {PACKAGE!r}
ALLOWED_FIRST_PARTY = {set(ALLOWED_FIRST_PARTY)!r}
ALLOWED_THIRD_PARTY = {set(ALLOWED_THIRD_PARTY)!r}
_ARTIFACT_ROOTS = {set(_ARTIFACT_ROOTS)!r}


def _is_runtime_artifact(root):
    return (
        root in _ARTIFACT_ROOTS
        or root.endswith("__mypyc")
        or root.startswith(("_cython_", "_sysconfigdata"))
    )


def _allowed(root):
    return (
        root in sys.stdlib_module_names
        or root in ALLOWED_FIRST_PARTY
        or root in ALLOWED_THIRD_PARTY
        or _is_runtime_artifact(root)
    )


class _StubApp:
    # Accepts every registration seam the plugin reaches for at import time:
    # attribute access yields another stub, a call with a single callable
    # argument behaves as a bare decorator, and any other call behaves as a
    # decorator factory.
    def __getattr__(self, name):
        return _StubApp()

    def __call__(self, *args, **kwargs):
        if len(args) == 1 and not kwargs and callable(args[0]):
            return args[0]
        return _StubApp()


tai42_app.bind(_StubApp())

package = importlib.import_module(PACKAGE)
for module_info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
    importlib.import_module(module_info.name)

offenders = sorted(
    root for root in {{name.partition(".")[0] for name in sys.modules}} if not _allowed(root)
)
for name in offenders:
    print(name)
"""


def _source_root() -> Path:
    root = Path(__file__).resolve().parents[1] / "src" / PACKAGE
    assert root.is_dir(), f"shipped package source not found at {root}"
    return root


def _static_import_roots() -> dict[str, set[str]]:
    """Map each import root in the shipped sources to the files that import it.

    Walks the full AST, so imports nested in a function, class body, or
    conditional are collected too. Relative imports carry no root to check.
    """
    roots: dict[str, set[str]] = {}
    source_root = _source_root()
    paths = sorted(source_root.rglob("*.py"))
    assert paths, f"no source files found under {source_root}"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for name in names:
                roots.setdefault(name.partition(".")[0], set()).add(str(path.relative_to(source_root)))
    return roots


def test_shipped_package_imports_only_allowlisted_roots() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing the shipped tai42_storage_github graph failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    offenders = [line for line in result.stdout.splitlines() if line]
    assert offenders == [], f"non-allowlisted roots in the tai42_storage_github module graph: {offenders}"


def test_shipped_sources_name_only_allowlisted_roots() -> None:
    offenders = {root: sorted(files) for root, files in _static_import_roots().items() if not _allowed(root)}
    assert offenders == {}, f"non-allowlisted import roots in the tai42_storage_github sources: {offenders}"
