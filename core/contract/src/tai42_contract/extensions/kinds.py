"""The shape of a tool extension, and the per-kind rules the platform enforces.

A tool extension extends a single tool (a plugin, by contrast, extends the
platform). Every extension is registered as a factory callable — it is handed
the tool's callable, its current name, and its description, and returns a
NEW-NAMED callable that the platform binds as a branch tool ALONGSIDE the
original. :class:`ExtensionFactory` names that shape.

Per-kind rules (keyed by :class:`ExtensionKind` properties, never by member):

* **wrapper** (``preserves_schema``) — presents the input schema it received
  unchanged, minus its own declared ``reserved_params`` (below). Stackable. A
  wrapper is schema-transparent in full: not just parameter names/types but
  defaults, descriptions, titles, and constraints must be identical — a
  deliberate default tweak is rejected.
* **transformer** (``declares_schema``) — presents its OWN composed input
  schema: a concrete signature (built with :func:`makefun.create_function`),
  never a bare ``(*args, **kwargs)`` passthrough. Stackable, applied
  left-to-right in manifest order.
* **backend** — one strategy per tool (``multiple=False``); no input-schema
  rule. Relocates execution (``relocates_execution``): the tool body runs in a
  worker process, so a layer that must run alongside the body (a
  ``requires_body_locality`` registration) binds inside it, never outside.

Stacking and ordering (settled by the apply site): a stacked combo ``[a, b]``
applies ``a`` first then ``b`` — left to right in combo order — producing
``b(a(tool))`` with ``b`` the outermost layer, and binds one branch tool per
prefix (``tool_a``, then ``tool_a_b``); the original ``tool`` stays bound too.
Every kind branches by rename; there is no in-place-replacement path.

``reserved_params`` (wrapper only): a wrapper may add control kwargs for itself
(e.g. a cache wrapper appending ``exp``). It declares them by carrying a
``reserved_params: frozenset[str]`` attribute on the factory callable; the
apply site subtracts each name from the branch's derived schema (from both
``properties`` and ``required``) before comparing with the layer's input
schema. Constraints:

* each reserved param's type must be schema-inline — a primitive or a plain
  container of primitives (``float``, ``list[str]``, ``dict[str, str]``); a
  pydantic-model-typed reserved param would leave a dangling ``$defs`` entry the
  subtraction cannot prune;
* a reserved name that also appears on the layer's input signature is a hard
  error at apply time (the exclusion would mask a genuine change to that param);
* ``reserved_params`` on a non-wrapper factory is ignored — a transformer owns
  its whole schema anyway.

A wrapper that appends a reserved param via makefun (e.g. cache's ``exp``) hits
makefun's own duplicate-parameter error INSIDE the factory when the name
collides with an existing input param, before the apply-site collision guard
runs; the guard is the safety net for a factory that declares a reserved name
without re-adding it, not the primary trap.

``runtime_checkable`` on :class:`ExtensionFactory` verifies only that
``__call__`` exists — it cannot check the 3-argument shape. It is documentation
plus a type-checking aid; real conformance is exercised by the apply-site call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExtensionFactory(Protocol):
    """The registered form of every tool extension: a factory called with the
    tool's callable, its current name, and its description, returning a
    NEW-NAMED callable that the platform binds as a branch tool alongside the
    original."""

    def __call__(self, func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]: ...


__all__ = ["ExtensionFactory"]
