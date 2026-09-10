"""The state-template seed registry and the seed applier — the module-side twin of the preset
applier — driven with an in-memory fake store (no live database)."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.states.models import StateTemplateDocument

from tai42_skeleton.states.seeds import StateTemplateSeedRegistry, apply_template_seeds


class _FakeSeedStore:
    """Records module upserts; ``present`` names read back as already stored."""

    def __init__(self, *, present: set[str] | None = None) -> None:
        self.present = present or set()
        self.upserts: list[tuple[str, dict[str, Any], str | None]] = []

    async def get_template(self, name: str):
        return {"name": name} if name in self.present else None

    async def upsert_template(self, name: str, body: dict[str, Any], shipped_hash: str | None) -> None:
        self.upserts.append((name, body, shipped_hash))
        self.present.add(name)


def test_registry_registers_and_lists() -> None:
    reg = StateTemplateSeedRegistry()
    a = StateTemplateDocument(name="a")
    b = StateTemplateDocument(name="b")
    reg.register(a)
    reg.register(b)
    assert {d.name for d in reg.seeds()} == {"a", "b"}


def test_registry_refuses_duplicate_name() -> None:
    reg = StateTemplateSeedRegistry()
    reg.register(StateTemplateDocument(name="a"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(StateTemplateDocument(name="a"))


def test_registry_reset_clears() -> None:
    reg = StateTemplateSeedRegistry()
    reg.register(StateTemplateDocument(name="a"))
    reg.reset()
    assert reg.seeds() == []


async def test_apply_seeds_creates_absent_and_stamps_hash() -> None:
    store = _FakeSeedStore()
    doc = StateTemplateDocument(name="shipped")
    await apply_template_seeds(store, seeds=[doc])  # type: ignore[arg-type]
    assert len(store.upserts) == 1
    name, body, shipped_hash = store.upserts[0]
    assert name == "shipped"
    assert body["name"] == "shipped"
    assert isinstance(shipped_hash, str)
    assert len(shipped_hash) == 64  # a sha256 hex digest


async def test_apply_seeds_leaves_present_untouched() -> None:
    store = _FakeSeedStore(present={"already"})
    await apply_template_seeds(store, seeds=[StateTemplateDocument(name="already")])  # type: ignore[arg-type]
    assert store.upserts == []  # idempotent — a present name is skipped


async def test_apply_seeds_hash_is_content_stable() -> None:
    doc = StateTemplateDocument(name="m")
    s1, s2 = _FakeSeedStore(), _FakeSeedStore()
    await apply_template_seeds(s1, seeds=[doc])  # type: ignore[arg-type]
    await apply_template_seeds(s2, seeds=[doc])  # type: ignore[arg-type]
    assert s1.upserts[0][2] == s2.upserts[0][2]  # the same body hashes identically
