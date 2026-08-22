"""SUT-side fixture package: modules the spawned tai server imports via its
manifest. This code runs INSIDE the system under test (never in the pytest
process), so it depends only on the ecosystem packages the SUT already has and
keeps module-top imports light.

The fixture connectors are now registered through the manifest ``connectors``
field, so the former ``tai42_e2e_fixtures.connector_provider`` lifecycle module
(which registered the ``e2e_idp``/``e2e_noauth_*`` providers on import via
``register_connector``) has been removed from the public surface."""
