"""The ``PluginSpec.migrations_component`` field (``tai42_contract.plugins``).

Pins the opt-in component override: it defaults to None (the chain runs under the
distribution name), accepts a name WHEN ``migrations`` is also declared, and is a
loud reject WITHOUT ``migrations`` (a component with no chain migrates nothing).
``extra="forbid"`` still holds, so a typo'd key fails rather than being ignored.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    # A minimal migrations-owning spec: one code (module-payload) item, so the
    # package requirement is met and ``migrations`` may be declared.
    base: dict[str, Any] = {
        "spec_version": 1,
        "namespace": "tai42",
        "name": "toolbox",
        "package": "tai42-toolbox",
        "version": "0.1.0",
        "description": "Generic tools and tool extensions.",
        "license": "Apache-2.0",
        "contract": ">=0.1,<0.2",
        "categories": ["utilities"],
        "provides": [
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "Generate a random UUID.",
            }
        ],
    }
    base.update(overrides)
    return base


def test_migrations_component_defaults_to_none():
    from tai42_contract.plugins import PluginSpec

    # Absent (base kwargs omit it) and an explicit None both leave the chain
    # running under the distribution name — byte-identical to prior behavior.
    assert PluginSpec(**_spec_kwargs()).migrations_component is None
    assert PluginSpec(**_spec_kwargs(migrations="migrations")).migrations_component is None
    assert PluginSpec(**_spec_kwargs(migrations="migrations", migrations_component=None)).migrations_component is None


def test_migrations_component_accepted_with_migrations():
    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_spec_kwargs(migrations="migrations", migrations_component="feature-store"))
    assert spec.migrations_component == "feature-store"


def test_migrations_component_without_migrations_rejected():
    from tai42_contract.plugins import PluginSpec

    # A component names WHERE a chain runs; naming it with no chain is a
    # meaningless declaration, rejected loudly rather than silently ignored.
    with pytest.raises(ValidationError, match="migrations_component requires migrations"):
        PluginSpec(**_spec_kwargs(migrations_component="feature-store"))


def test_spec_still_forbids_extra_keys():
    from tai42_contract.plugins import PluginSpec

    # The field addition does not loosen extra="forbid": a typo'd key still fails.
    with pytest.raises(ValidationError):
        PluginSpec(**_spec_kwargs(migrations="migrations", migrations_componnt="feature-store"))
