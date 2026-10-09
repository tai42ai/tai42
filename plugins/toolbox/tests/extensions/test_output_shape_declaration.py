"""The ``output_schema`` extension declares the wrapped tool's output shape at registration."""

from __future__ import annotations

import tai42_toolbox.extensions.output_schema as output_schema_module

from .conftest import capture_extensions


def test_the_output_schema_extension_declares_the_output_shape() -> None:
    assert capture_extensions(output_schema_module).declares_output_schema == {"output_schema": True}
