"""The shared base's mechanical certification of this backend's declarations."""

from tai42_kit.backend import check_backend_declarations

from tai42_backend_arq.backend import ArqBackend


def test_backend_declarations_are_conformant() -> None:
    assert check_backend_declarations(ArqBackend()) == []
