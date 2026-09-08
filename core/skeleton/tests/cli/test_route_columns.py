"""Drift + parity gate for the CLI's generated route->column table.

The ``tai42-cli`` package ships a generated ``commands._route_columns`` table so the
standalone client can render model-derived tables without importing the skeleton
registry it is banned from. This gate — which runs in the skeleton closure, where
BOTH packages are importable — keeps that table honest:

* **Drift** — re-derive the table from the live registry and assert the committed
  table matches it field-for-field, so a renamed/removed model field (or a new list
  route) reds here instead of silently mis-rendering.
* **Coverage** — every ``emit_records(..., route=(M, P))`` call resolves to a real
  table entry (no orphan route), and names a route the command actually ``@covers``.
* **No silent hand-fallback** — a command whose route HAS a derivable model must
  derive from it; a hand-written ``columns=`` is allowed only where the route yields
  no single-list table (an opaque body, a multi-list envelope, a locally reshaped
  payload).
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

# Importing the app imports every command module (needed for the router universe
# ``load_api_routes`` enumerates, and it is the surface these calls live on).
import tai42_cli.app  # noqa: F401
from tai42_cli.commands import _common, _route_columns

_COMMANDS_DIR = Path(_common.__file__).parent
_GEN_PATH = Path(__file__).with_name("_gen_route_columns.py")


def _load_generator():
    spec = importlib.util.spec_from_file_location("_gen_route_columns", _GEN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _as_plain(shapes) -> dict[tuple[str, str], tuple[str | None, tuple[str, ...]]]:
    # Compare by value across the two RouteShape NamedTuple classes (the committed
    # one and the generator's), independent of formatting or class identity.
    return {key: (shape.items_key, tuple(shape.columns)) for key, shape in shapes.items()}


def test_committed_table_matches_the_live_derivation() -> None:
    generator = _load_generator()
    derived = _as_plain(generator.derive_shapes())
    committed = _as_plain(_route_columns.ROUTE_TABLE_SHAPES)
    assert committed == derived, (
        "commands/_route_columns.py is stale — regenerate it with "
        "`python core/skeleton/tests/cli/_gen_route_columns.py` (then `ruff format`)."
    )


class _EmitCall:
    __slots__ = ("hand_written", "route")

    def __init__(self, route: tuple[str, str] | None, hand_written: bool) -> None:
        self.route = route
        self.hand_written = hand_written


def _route_tuple(node: ast.AST) -> tuple[str, str] | None:
    if (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in node.elts)
    ):
        return (node.elts[0].value, node.elts[1].value)  # type: ignore[attr-defined]
    return None


def _covers_routes(func: ast.FunctionDef) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for decorator in func.decorator_list:
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == "covers":
            for arg in decorator.args:
                pair = _route_tuple(arg)
                if pair is not None:
                    routes.add((pair[0].upper(), pair[1]))
    return routes


def _emit_calls(func: ast.FunctionDef) -> list[_EmitCall]:
    calls: list[_EmitCall] = []
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "emit_records"):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        route = None
        if "route" in keywords:
            route = _route_tuple(keywords["route"])
            assert route is not None, f"emit_records route= must be a literal (METHOD, PATH) tuple in {func.name}"
            route = (route[0].upper(), route[1])
        has_columns = len(node.args) >= 3 or "columns" in keywords
        calls.append(_EmitCall(route=route, hand_written=has_columns and route is None))
    return calls


def _renders_via_emit_result(func: ast.FunctionDef) -> bool:
    """Whether the command renders a response with ``emit_result`` (a whole object, not a
    table). A route it ``@covers`` and renders this way is legitimately not table-derived,
    so its derivable model does not make a sibling hand-written ``emit_records`` a
    silent fallback."""
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "emit_result"
        for node in ast.walk(func)
    )


def _command_functions() -> list[tuple[str, ast.FunctionDef]]:
    functions: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(_COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "emit_records"
                for sub in ast.walk(node)
            ):
                functions.append((f"{path.name}::{node.name}", node))
    return functions


def test_derived_calls_resolve_to_a_real_table_entry() -> None:
    orphans: list[str] = []
    for label, func in _command_functions():
        for call in _emit_calls(func):
            if call.route is not None and call.route not in _route_columns.ROUTE_TABLE_SHAPES:
                orphans.append(f"{label}: route={call.route}")
    assert not orphans, f"emit_records route= with no generated table entry: {orphans}"


def test_derived_calls_name_a_covered_route() -> None:
    mismatched: list[str] = []
    for label, func in _command_functions():
        covers = _covers_routes(func)
        for call in _emit_calls(func):
            if call.route is not None and call.route not in covers:
                mismatched.append(f"{label}: route={call.route} not in @covers {sorted(covers)}")
    assert not mismatched, f"emit_records route= that the command does not @covers: {mismatched}"


def test_no_silent_hand_fallback_where_a_model_exists() -> None:
    # A hand-written columns= is legitimate ONLY where the command's route yields no
    # derivable single-list table. If a derivable route is available and unconsumed by
    # a route= call in the same command, the hand-written list is masking a model.
    offenders: list[str] = []
    for label, func in _command_functions():
        calls = _emit_calls(func)
        if not any(call.hand_written for call in calls):
            continue
        # A command that also renders with emit_result covers a route it shows as a whole
        # object, not a table; that derivable route is not the one the hand-written
        # emit_records masks, so it must not count as an unconsumed model (fail-closed:
        # a command that ONLY table-renders still reds if it hand-writes a derivable route).
        if _renders_via_emit_result(func):
            continue
        derived_here = {call.route for call in calls if call.route is not None}
        derivable = {route for route in _covers_routes(func) if route in _route_columns.ROUTE_TABLE_SHAPES}
        unconsumed = derivable - derived_here
        if unconsumed:
            offenders.append(f"{label}: hand-written emit_records but derivable route(s) exist: {sorted(unconsumed)}")
    assert not offenders, f"silent hand-fallback where a response model could derive the columns: {offenders}"
