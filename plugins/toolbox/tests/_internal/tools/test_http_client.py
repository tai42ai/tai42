"""Test that the ``request`` tool's execution helper raises the ``http`` extra
install hint when the curl backend is absent.

The serialize/decode/streaming paths themselves are exercised end-to-end through
the ``request`` tool in ``test_request`` and ``test_url_guard``."""

from __future__ import annotations

import importlib
from collections.abc import Callable

import pytest


def test_missing_extra_raises_install_hint(
    force_missing_import: Callable[[str, list[str]], None],
) -> None:
    force_missing_import("curl_cffi", ["tai42_toolbox._internal.tools.http_client", "tai42_kit.clients.impl.curl"])
    with pytest.raises(ImportError, match=r"tai42-toolbox\[http\]"):
        importlib.import_module("tai42_toolbox._internal.tools.http_client")
