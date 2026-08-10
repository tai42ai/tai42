"""Import-graph guard for the shipped package.

Two complementary walks assert the same rule: every import root reachable from
``tai42_backend_arq`` is on the allowlist (the broker SDK, the shared platform
substrate, their dependency closure, and the standard library only). The runtime
walk imports the package and every submodule in a fresh subprocess and inspects
``sys.modules``; the static walk parses every source file and collects import
roots at any nesting depth, catching imports nested in functions or
``TYPE_CHECKING`` blocks that never execute on a plain import. Both walks share
one allowlist.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

# The shipped package and the public first-party packages it may import.
PACKAGE = "tai42_backend_arq"
ALLOWED_FIRST_PARTY = frozenset({PACKAGE, "tai42_contract", "tai42_kit"})

# Every third-party root the shipped ``tai42_backend_arq`` graph pulls in — the
# declared runtime dependencies plus their resolved closure. Compiled extension
# modules are listed under the bare top-level name they register (e.g.
# ``_cffi_backend``, ``_openssl``). Add a root here only for a genuine dependency
# of the shipped package.
ALLOWED_THIRD_PARTY = frozenset(
    {
        "_cffi_backend",
        "_openssl",
        "annotated_types",
        "anyio",
        "arq",
        "attr",
        "attrs",
        "certifi",
        "click",
        "croniter",
        "cryptography",
        "dateutil",
        "dotenv",
        "fastmcp",
        "h11",
        "hiredis",
        "httpcore",
        "httpx",
        "idna",
        "jsonschema",
        "jsonschema_specifications",
        "jwt",
        "makefun",
        "orjson",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "pygments",
        "redis",
        "referencing",
        "rich",
        "rpds",
        "ruamel",
        "six",
        "typing_extensions",
        "typing_inspection",
    }
)

# Interpreter/compiler/virtual-env roots that land in ``sys.modules`` as ambient
# side effects. Not dependency packages; their exact names are
# build/platform/version specific, so they are matched by shape, never by literal.
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


# Program run in the subprocess: bind a stub app to the ``tai42_app`` handle,
# import the package and every submodule, then print each imported root NOT on
# the allowlist. A submodule that fails to import gives a non-zero exit.
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

    Walks the full AST of every source file, so an import nested inside a
    function body, a class body, or a conditional block is collected exactly
    like a module-level one. Relative imports address the shipped package
    itself and carry no root to check.
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
        f"importing the shipped tai42_backend_arq graph failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    offenders = [line for line in result.stdout.splitlines() if line]
    assert offenders == [], f"non-allowlisted roots in the tai42_backend_arq module graph: {offenders}"


def test_shipped_sources_name_only_allowlisted_roots() -> None:
    offenders = {root: sorted(files) for root, files in _static_import_roots().items() if not _allowed(root)}
    assert offenders == {}, f"non-allowlisted import roots in the tai42_backend_arq sources: {offenders}"
