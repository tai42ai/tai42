"""Tests for the ``pad_embeddings`` tool: zero-padding to a fixed width and its
registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_toolbox.tools.pad_embeddings import pad_embeddings


def test_pad_embeddings_pads_single_vector() -> None:
    assert pad_embeddings([1.0, 2.0], target_dim=4) == [[1.0, 2.0, 0.0, 0.0]]


def test_pad_embeddings_pads_a_batch() -> None:
    assert pad_embeddings([[1.0], [2.0, 3.0]], target_dim=3) == [[1.0, 0.0, 0.0], [2.0, 3.0, 0.0]]


def test_pad_embeddings_empty_input_returns_empty() -> None:
    assert pad_embeddings([], target_dim=4) == []


def test_pad_embeddings_rejects_a_wider_vector() -> None:
    with pytest.raises(ValueError, match="must be >="):
        pad_embeddings([1.0, 2.0, 3.0], target_dim=2)


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_toolbox.tools.pad_embeddings")
    assert set(app.tools.registered) == {"pad_embeddings"}
