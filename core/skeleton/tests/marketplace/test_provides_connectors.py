"""Which connector providers a plugin spec writes into the manifest."""

from __future__ import annotations

from tai42_skeleton.marketplace.provides import connector_provider_ids

from ._specs import connector_item, make_spec, tool_item


def test_the_connector_ids_a_spec_provides_in_order() -> None:
    spec = make_spec(
        namespace="acme",
        package=None,
        provides=[
            connector_item("kappa", origin="community", discovered=True),
            connector_item("iota", origin="community", kind="none"),
        ],
    )
    assert connector_provider_ids(spec) == ["kappa", "iota"]


def test_a_spec_without_connectors_provides_none() -> None:
    assert connector_provider_ids(make_spec(provides=[tool_item()])) == []
