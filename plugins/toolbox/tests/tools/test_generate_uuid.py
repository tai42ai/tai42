"""Tests for the ``generate_uuid`` tool: UUID generation and its registration."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from tai42_toolbox.tools.generate_uuid import generate_uuid


def test_generate_uuid_is_a_version_4_uuid() -> None:
    value = generate_uuid()
    parsed = uuid.UUID(value)
    assert str(parsed) == value
    assert parsed.version == 4


def test_generate_uuid_is_unique_per_call() -> None:
    assert generate_uuid() != generate_uuid()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_toolbox.tools.generate_uuid")
    assert set(app.tools.registered) == {"generate_uuid"}
