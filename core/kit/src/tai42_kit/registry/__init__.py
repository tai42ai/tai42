"""Staged-generation registry primitives shared by every rebuildable process-global registry."""

from tai42_kit.registry.staged import NamedFactoryRegistry, StagedGeneration, StagedSlot, same_factory

__all__ = ["NamedFactoryRegistry", "StagedGeneration", "StagedSlot", "same_factory"]
