"""A stand-alone Postgres read runs on ``read_connection``, never on a transactional ``pool.connection()``.

``pool.connection()`` opens psycopg's implicit transaction, so a plain read pays a ``BEGIN`` and a
``COMMIT`` around its statements. Every function in the skeleton that opens ``pool.connection()``
must therefore open ``conn.transaction()``, or run a statement that is not a plain ``SELECT`` (a
write, a locking read), or be one of the named exceptions; a new stand-alone read on
``pool.connection()`` fails here, naming the function.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import tai42_skeleton

_SKELETON_SRC = Path(tai42_skeleton.__file__).parent

# Functions that open ``pool.connection()`` for a plain read on purpose.
_EXCEPTIONS = {
    # The run index's one cursor serves its index writes too.
    ("runs/store.py", "_cursor"),
    # The liveness probes prove the ordinary connection path as it is.
    ("routers/health.py", "_ping_postgres"),
    ("cli/native/doctor.py", "_probe_postgres"),
}

_SQL = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|LOCK|MERGE|COPY|SET|DO)\b", re.I)
_NOT_A_PLAIN_SELECT = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|LOCK|MERGE|COPY|DO)\b"
    r"|\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b|pg_advisory",
    re.I,
)


def _strings(node: ast.AST) -> list[str]:
    return [sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]


def _opens_pool_connection(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "connection"
        and isinstance(sub.func.value, ast.Name)
        and sub.func.value.id == "pool"
        for sub in ast.walk(node)
    )


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]


def _sql_of(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    constants: dict[str, list[str]],
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[str]:
    """Every SQL text ``fn`` runs: its own literals, the module constants it names, the module methods it calls."""
    texts = _strings(fn)
    for name in {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}:
        texts += constants.get(name, [])
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in methods:
            helper = methods[call.func.attr]
            if helper is not fn:
                texts += _strings(helper)
    return [text for text in texts if _SQL.match(text)]


def _standalone_reads_on_pool_connection() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_SKELETON_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(_SKELETON_SRC).as_posix()
        constants: dict[str, list[str]] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and node.value is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = _strings(node.value)
        functions = _functions(tree)
        methods = {fn.name: fn for fn in functions}
        for fn in functions:
            nested = [inner for inner in _functions(fn) if inner is not fn]
            if not _opens_pool_connection(fn) or any(_opens_pool_connection(inner) for inner in nested):
                continue
            if (relative, fn.name) in _EXCEPTIONS:
                continue
            opens_transaction = any(
                isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "transaction"
                for call in ast.walk(fn)
            )
            sql = _sql_of(fn, constants, methods)
            if opens_transaction or any(_NOT_A_PLAIN_SELECT.search(text) for text in sql):
                continue
            offenders.append(f"{relative}:{fn.lineno} {fn.name}")
    return offenders


def test_every_standalone_read_runs_on_read_connection() -> None:
    offenders = _standalone_reads_on_pool_connection()
    assert offenders == [], "stand-alone reads on pool.connection() (use read_connection):\n" + "\n".join(offenders)


def test_the_scan_sees_a_standalone_read() -> None:
    tree = ast.parse(
        "async def read_one(pool):\n"
        "    async with pool.connection() as conn, conn.cursor() as cur:\n"
        "        await cur.execute('SELECT 1')\n"
    )
    (fn,) = _functions(tree)
    assert _opens_pool_connection(fn)
    assert _sql_of(fn, {}, {}) == ["SELECT 1"]
