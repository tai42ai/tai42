# iota fixture connector

Descriptor-only OAuth connector marketplace fixture. Ships no package: the
`tai-plugin.yml.tmpl` is rendered per stack (the `{{IDP_BASE_URL}}` token resolves
to the harness OAuth/MCP stub, `{{NAME}}`/`{{VERSION}}`/`{{SCOPES}}` to the
seeded listing's identity) and the rendered bytes are served by the github-API
stub at the release tag, digested `source='spec'` by the registry.

Two versions publish: `0.1.0` (one scope) and `0.2.0` (adds a scope). The install
requires `CONNECTORS_IOTA_CLIENT_ID` and `CONNECTORS_IOTA_CLIENT_SECRET`; the
secret is auto-masked.
