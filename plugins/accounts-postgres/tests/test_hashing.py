"""argon2 hashing wrapper + the off-loop concurrency gate."""

from __future__ import annotations

import pytest

from tai42_accounts_postgres import hashing
from tai42_accounts_postgres.hashing import (
    DUMMY_HASH,
    HashCapacityError,
    HashGate,
    hash_password,
    verify_password,
)


async def test_hash_then_verify_round_trips():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert await verify_password(h, "correct horse battery staple") is True


async def test_verify_mismatch_returns_false():
    h = hash_password("right-password")
    assert await verify_password(h, "wrong-password") is False


async def test_dummy_hash_verify_is_false_but_real_work():
    assert await verify_password(DUMMY_HASH, "anything") is False


async def test_gate_sheds_when_saturated():
    """A gate that can admit nobody sheds with HashCapacityError rather than
    queueing or running the hash."""
    hashing._gate = HashGate(0, 0.01)
    try:
        with pytest.raises(HashCapacityError):
            await verify_password(hash_password("x" * 12), "x" * 12)
    finally:
        hashing.reset_hash_gate()


async def test_gate_admits_within_capacity():
    hashing._gate = HashGate(2, 1.0)
    try:
        assert await verify_password(hash_password("y" * 12), "y" * 12) is True
    finally:
        hashing.reset_hash_gate()
