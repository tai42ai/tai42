"""SUT-side fixture package: modules the spawned tai server imports via its
manifest. This code runs INSIDE the system under test (never in the pytest
process), so it depends only on the ecosystem packages the SUT already has and
keeps module-top imports light.

The fixture connectors (``e2e_idp``, ``e2e_noauth_*``) are registered through
the manifest ``connectors`` field, never by import-time side effects."""
