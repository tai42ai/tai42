"""Test that the chain executor module raises the ``chain`` extra install hint
when the jq library is absent."""

import importlib
import sys

import pytest

import tai42_toolbox._internal.extensions.chain_executor as chain_executor_module


def test_missing_extra_raises_install_hint(monkeypatch):
    # Simulate the chain extra (jq) being absent: the module must fail loudly at
    # import with a copy-pasteable install hint, never a silent skip.
    monkeypatch.setitem(sys.modules, "jq", None)
    with pytest.raises(ImportError, match=r"tai42-toolbox\[chain\]"):
        importlib.reload(chain_executor_module)

    # Restore a clean module for the rest of the suite.
    monkeypatch.undo()
    importlib.reload(chain_executor_module)
