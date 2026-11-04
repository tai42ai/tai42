"""The shared-base certification, run against the shipped RQ backend.

An empty problem list is the claim that this plugin writes only its runtime
classes: the readiness gate, the composed warm/cold shutdown wiring, the
drain-on-cancellation and the off-loop run all come from the base, no runtime
installs a signal handler of its own, and the consuming runtime carries the
canonical name the host sniffs argv for before this module is even imported. The
backend also registers the whole ``backend_*`` tool surface, each tool tagged ``backend``.
"""

from __future__ import annotations

from typing import Any

from tai42_kit.backend import BACKEND_TOOLS, check_backend_declarations, check_tool_surface
from tai42_kit.utils.lc.signature_util import resolved_type_hints

from tai42_backend_rq.backend import RqBackend

_RqBackendCls: Any = RqBackend


def test_the_rq_backend_is_certified() -> None:
    assert check_backend_declarations(_RqBackendCls()) == []


def test_the_rq_backend_registers_the_whole_backend_tool_surface(app) -> None:
    assert check_tool_surface(app.tools.tags) == []


def test_every_backend_tool_belongs_to_the_rq_package(app) -> None:
    # The host admits the tools of the package the manifest's ``backend_module`` names, by the
    # module each tool function names; its annotations resolve in that module.
    for name in BACKEND_TOOLS:
        tool = app.tools.registered[name]
        assert tool.__module__ == "tai42_backend_rq", name
        resolved_type_hints(tool)
