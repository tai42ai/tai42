# kappa fixture connector

Descriptor-only no-auth (`kind: none`) connector marketplace fixture. Ships no
package: the `tai-plugin.yml.tmpl` is rendered per stack (the `{{PYTHON}}` token
resolves to the SUT interpreter that launches the managed stdio MCP server) and
the rendered bytes are served by the github-API stub at the release tag, digested
`source='spec'` by the registry.

Installing it requires NO env (no OAuth client credentials); the two
`config_fields` are supplied by the end user at connect time and injected into the
managed server's stdio env.
