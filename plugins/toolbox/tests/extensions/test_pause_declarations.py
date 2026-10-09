"""The ``chain`` and ``batch`` extensions register as pausing: every branch they mint can pause.

A branch passes a stage's park through (``chain``'s next stage is chosen at call time), so a branch can pause
whatever its base declares; the platform then answers ``pauses`` for each such branch.
"""

import pytest

import tai42_toolbox.extensions.batch as batch_module
import tai42_toolbox.extensions.chain as chain_module

from .conftest import capture_extensions


@pytest.mark.parametrize(("module", "name"), [(chain_module, "chain"), (batch_module, "batch")])
def test_the_extension_registers_as_pausing(module, name):
    assert capture_extensions(module).pauses == {name: True}
