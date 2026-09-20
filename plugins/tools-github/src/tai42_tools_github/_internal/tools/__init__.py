"""Private helper modules backing the GitHub provisioning tools.

Not part of the public API: the module here holds the shared GitHub REST client (repository-webhook
create/list/delete, token auth, and Link-header pagination) that the registered entrypoints in
``tai42_tools_github.tools`` delegate to. Nothing here registers through ``tai42_app``.
"""
