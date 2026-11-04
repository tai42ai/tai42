"""The shared base's mechanical certification of this backend's declarations and tool surface."""

from tai42_kit.backend import BACKEND_TOOLS, check_backend_declarations, check_tool_surface
from tai42_kit.utils.lc.signature_util import resolved_type_hints

from tai42_backend_arq.backend import ArqBackend

from .conftest import StubApp


def test_backend_declarations_are_conformant() -> None:
    assert check_backend_declarations(ArqBackend()) == []


def test_the_arq_backend_registers_the_whole_backend_tool_surface(stub_app: StubApp) -> None:
    assert check_tool_surface(stub_app.tools.tags) == []


def test_every_backend_tool_belongs_to_the_arq_package(stub_app: StubApp) -> None:
    # The host admits the tools of the package the manifest's ``backend_module`` names, by the
    # module each tool function names; its annotations resolve in that module.
    for name in BACKEND_TOOLS:
        tool = stub_app.tools.registered[name]
        assert tool.__module__ == "tai42_backend_arq", name
        resolved_type_hints(tool)
