"""The shared-base certification, run against this backend.

An empty problem list is the claim that this plugin writes only its Celery
bindings and gets readiness gating, composed shutdown wiring,
drain-on-cancellation and pool turnover from the base — with no host-shaped code,
and no signal handler, of its own; and that it registers the whole ``backend_*`` tool
surface, every tool tagged ``backend``.
"""

from __future__ import annotations

from tai42_kit.backend import BACKEND_TOOLS, check_backend_declarations, check_tool_surface
from tai42_kit.utils.lc.signature_util import resolved_type_hints

from tai42_backend_celery.core.backend import CeleryBackend


def test_the_celery_backend_is_certified() -> None:
    assert check_backend_declarations(CeleryBackend()) == []


def test_the_celery_backend_registers_the_whole_backend_tool_surface(stub_app) -> None:
    assert check_tool_surface(stub_app.tools.tags) == []


def test_every_backend_tool_belongs_to_the_celery_package(stub_app) -> None:
    # The host admits the tools of the package the manifest's ``backend_module`` names, by the
    # module each tool function names; its annotations resolve in that module.
    for name in BACKEND_TOOLS:
        tool = stub_app.tools.registered[name]
        assert tool.__module__ == "tai42_backend_celery", name
        resolved_type_hints(tool)
